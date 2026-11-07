import asyncio
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge_server import Bridge, handle_http, handle_tcp, MAX_DELIVER_CHARS


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridge = Bridge(Path(self.temp.name))

    async def test_wait_receives_once_and_routes_to_recipient(self):
        b = self.bridge
        await b.dispatch({"op": "send", "agent": "claude", "to": "reviewer", "text": "private review"})
        pending = asyncio.create_task(b.dispatch({"op": "wait", "agent": "codex", "timeout": 1}))
        await asyncio.sleep(.02)
        sent = await b.dispatch({"op": "send", "agent": "claude", "text": "review this"})
        received = await pending
        self.assertEqual([m["id"] for m in received["messages"]], [sent["id"]])
        again = await b.dispatch({"op": "wait", "agent": "codex", "timeout": 1})
        self.assertEqual(again["messages"], [])

    async def test_compaction_truncation_and_restart(self):
        b = self.bridge
        full = "x" * (MAX_DELIVER_CHARS + 21000)
        await b.dispatch({"op": "send", "agent": "claude", "text": full})
        self.assertTrue(b.compact_pending)
        restored = Bridge(Path(self.temp.name))
        self.assertTrue(restored.compact_pending)
        self.assertIn("truncated", restored.context_for("codex")["recent"][0]["text"])
        self.assertEqual((await restored.dispatch({"op": "get", "id": 1}))["message"]["text"], full)
        await restored.dispatch({"op": "compact", "agent": "claude", "text": "Summary"})
        final = Bridge(Path(self.temp.name))
        self.assertEqual(final.summary, "Summary")
        self.assertFalse(final.compact_pending)
        self.assertEqual(len(final.context_for("codex")["recent"]), 1)

    async def test_stop_unblocks_waiter_and_reset_archives(self):
        b = self.bridge
        waiting = asyncio.create_task(b.dispatch({"op": "wait", "agent": "codex", "timeout": 10}))
        await asyncio.sleep(.02)
        await b.dispatch({"op": "stop"})
        self.assertTrue((await waiting)["stopped"])
        await b.dispatch({"op": "reset"})
        await b.dispatch({"op": "send", "agent": "claude", "text": "second"})
        await b.dispatch({"op": "reset"})
        self.assertEqual(len(list(Path(self.temp.name).glob("transcript-*.jsonl"))), 2)
        self.assertFalse(b.stopped)
        self.assertEqual(b.messages, [])

    async def test_concurrent_ids_and_invalid_requests(self):
        results = await asyncio.gather(*(self.bridge.dispatch({"op": "send", "agent": "claude", "text": str(i)}) for i in range(20)))
        self.assertEqual(len({r["id"] for r in results}), 20)
        for req in ([], {"op": "unknown"}, {"op": "hello"}, {"op": "send", "agent": [], "text": "x"},
                    {"op": "send", "agent": "codex", "text": 1}):
            self.assertFalse((await self.bridge.dispatch(req))["ok"])

    async def test_tcp_handles_large_message_and_recovers_from_bad_json(self):
        server = await asyncio.start_server(lambda r, w: handle_tcp(self.bridge, r, w), "127.0.0.1", 0, limit=1024*1024)
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
            writer.write(b"not json\n")
            await writer.drain()
            self.assertFalse(json.loads(await reader.readline())["ok"])
            writer.write((json.dumps({"op": "send", "agent": "claude", "text": "x" * 70000}) + "\n").encode())
            await writer.drain()
            self.assertTrue(json.loads(await reader.readline())["ok"])
            writer.close()
            await writer.wait_closed()

    async def test_http_same_origin_checks_and_controls(self):
        server = await asyncio.start_server(lambda r, w: handle_http(self.bridge, r, w), "127.0.0.1", 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            async def request(method, path, extra="", host=None):
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(f"{method} {path} HTTP/1.1\r\nHost: {host or f'127.0.0.1:{port}'}\r\n{extra}\r\n".encode())
                await writer.drain()
                data = await reader.read()
                writer.close()
                await writer.wait_closed()
                return data
            self.assertIn(b"200 OK", await request("GET", "/"))
            self.assertIn(b"403 Forbidden", await request("GET", "/api/state", host="attacker.example"))
            self.assertIn(b"403 Forbidden", await request("POST", "/api/stop", "Origin: https://attacker.example\r\nContent-Type: application/json\r\n"))
            self.assertIn(b"415 Unsupported", await request("POST", "/api/stop"))
            self.assertFalse(self.bridge.stopped)
            self.assertIn(b"200 OK", await request("POST", "/api/stop", "Content-Type: application/json\r\n"))
            self.assertTrue(self.bridge.stopped)


class InstallationTests(unittest.TestCase):
    def test_real_server_and_cli_round_trip(self):
        # Allocate test-only ports; never contact an installed default server.
        sockets = [socket.socket(), socket.socket()]
        for sock in sockets:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in sockets]
        for sock in sockets:
            sock.close()
        env = dict(os.environ, BRIDGE_HOST="127.0.0.1", BRIDGE_PORT=str(ports[0]), BRIDGE_GUI_PORT=str(ports[1]))
        with tempfile.TemporaryDirectory() as data:
            proc = subprocess.Popen([sys.executable, str(ROOT / "bridge_server.py"), "--data", data], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                def cli(*args):
                    return subprocess.run([sys.executable, str(ROOT / "bridge.py"), *args], env=env,
                                          capture_output=True, text=True, encoding="utf-8", timeout=5)
                for attempt in range(30):
                    if proc.poll() is not None:
                        self.fail("test server exited before startup")
                    if cli("status").returncode == 0:
                        break
                    time.sleep(.05)
                else:
                    self.fail("test server did not start")
                self.assertEqual(cli("join", "--as", "codex").returncode, 0)
                message = Path(data) / "message.txt"
                message.write_text("Review café\nsecond line", encoding="utf-8")
                self.assertEqual(cli("send", "--as", "claude", "--file", str(message)).returncode, 0)
                received = cli("wait", "--as", "codex", "--timeout", "1")
                self.assertEqual(received.returncode, 0, received.stderr)
                self.assertIn("Review café\nsecond line", received.stdout)
                self.assertEqual(cli("wait", "--as", "codex", "--timeout", "1").returncode, 2)
                self.assertEqual(cli("stop").returncode, 0)
                self.assertEqual(cli("wait", "--as", "codex", "--timeout", "1").returncode, 0)
                self.assertEqual(cli("wait", "--as", "codex", "--timeout", "1").returncode, 3)
            finally:
                proc.terminate()
                proc.wait(timeout=5)

    def test_install_handles_spaces_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory(prefix="agent connect ") as home:
            args = [sys.executable, str(ROOT / "install_skills.py"), "--home", home]
            first = subprocess.run(args, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            skill = Path(home) / ".agents/skills/agent-connect/SKILL.md"
            self.assertIn(str(ROOT).replace("\\", "\\\\"), skill.read_text(encoding="utf-8"))
            self.assertNotEqual(subprocess.run(args, capture_output=True).returncode, 0)
            self.assertEqual(subprocess.run(args + ["--force"], capture_output=True).returncode, 0)

    def test_cli_help_and_non_loopback_rejected(self):
        for name in ("bridge.py", "bridge_server.py", "install_skills.py"):
            result = subprocess.run([sys.executable, str(ROOT / name), "--help"], capture_output=True)
            self.assertEqual(result.returncode, 0)
        result = subprocess.run([sys.executable, str(ROOT / "bridge_server.py"), "--host", "0.0.0.0"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("loopback", result.stderr)


if __name__ == "__main__":
    unittest.main()
