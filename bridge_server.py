"""Agent bridge server.

TCP relay (default 127.0.0.1:8765) that lets two coding agents (Claude Code and
Codex) exchange messages, plus a read-only web GUI (default 127.0.0.1:8766)
that shows the conversation live.

Wire protocol: the client sends one JSON object per line, the server answers
with one JSON object per line. See bridge.py for the client.

Compaction: the server keeps the full transcript on disk, but what an agent is
handed is kept small:
  * long messages are truncated on delivery (full text via the "get" op),
  * "context" returns the task, the latest summary and only the messages
    since that summary,
  * once the un-summarized history grows past a threshold, the next agent to
    receive a message is asked to post a summary ("compact" op). Everything
    before the summary is then dropped from what agents see.

Stdlib only. Run: python bridge_server.py
"""

import argparse
import asyncio
import ipaddress
import json
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent

# Delivery / compaction limits
MAX_DELIVER_CHARS = 4000       # truncate single messages longer than this on delivery
COMPACT_CHARS = 20000          # ask for a summary once live history exceeds this
COMPACT_MESSAGES = 24          # ...or this many messages
RECENT_IN_CONTEXT = 8          # max messages returned by "context"
ONLINE_WINDOW = 90             # seconds since last contact to count as online
MAX_REQUEST_BYTES = 1024 * 1024


class Bridge:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = data_dir / "transcript.jsonl"
        self.state_path = data_dir / "state.json"
        self.messages = []          # every message ever, in order
        self.task = ""
        self.summary = ""
        self.summary_upto = 0       # messages with id <= this are compacted
        self.compact_pending = False
        self.cursors = {}           # agent -> last delivered message id
        self.agents = {}            # agent -> {"last_seen": ts, "waiting": bool}
        self.stopped = False
        self.cond = asyncio.Condition()
        self.gui_queues = set()
        self._load()
        self.compact_pending = self.needs_compaction()

    # ---------- persistence ----------
    def _load(self):
        if self.log_path.exists():
            with self.log_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.messages.append(json.loads(line))
        if self.state_path.exists():
            st = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.task = st.get("task", "")
            self.summary = st.get("summary", "")
            self.summary_upto = st.get("summary_upto", 0)
            self.cursors = st.get("cursors", {})
            self.stopped = st.get("stopped", False)

    def _save_state(self):
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "task": self.task,
            "summary": self.summary,
            "summary_upto": self.summary_upto,
            "cursors": self.cursors,
            "stopped": self.stopped,
        }, indent=2), encoding="utf-8")
        temporary.replace(self.state_path)

    def _append(self, msg):
        self.messages.append(msg)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    # ---------- helpers ----------
    def next_id(self):
        return self.messages[-1]["id"] + 1 if self.messages else 1

    def live_messages(self):
        return [m for m in self.messages if m["id"] > self.summary_upto]

    def touch(self, agent, waiting=None):
        a = self.agents.setdefault(agent, {"last_seen": 0, "waiting": False})
        a["last_seen"] = time.time()
        if waiting is not None:
            a["waiting"] = waiting

    def agent_status(self):
        now = time.time()
        return {
            name: {
                "online": a["waiting"] or now - a["last_seen"] < ONLINE_WINDOW,
                "waiting": a["waiting"],
                "last_seen": a["last_seen"],
            }
            for name, a in self.agents.items()
        }

    def needs_compaction(self):
        live = [m for m in self.live_messages() if m["kind"] != "system"]
        chars = sum(len(m["text"]) for m in live)
        return len(live) >= COMPACT_MESSAGES or chars >= COMPACT_CHARS

    @staticmethod
    def for_delivery(m):
        text = m["text"]
        if len(text) > MAX_DELIVER_CHARS:
            text = (text[:MAX_DELIVER_CHARS]
                    + f"\n...[truncated {len(m['text']) - MAX_DELIVER_CHARS} chars;"
                    f" run: python bridge.py get {m['id']}]")
        return {**m, "text": text}

    def is_for(self, m, agent):
        if m["from"] == agent:
            return False
        return m["to"] in (agent, "all", "*") or (m["to"] == "other")

    def snapshot(self):
        return {
            "task": self.task,
            "summary": self.summary,
            "summary_upto": self.summary_upto,
            "stopped": self.stopped,
            "agents": self.agent_status(),
            "messages": self.messages[-400:],
            "live_count": len(self.live_messages()),
            "live_chars": sum(len(m["text"]) for m in self.live_messages()),
        }

    async def broadcast_gui(self, event):
        for q in list(self.gui_queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    async def add_message(self, frm, to, text, kind="msg"):
        async with self.cond:
            msg = {"id": self.next_id(), "ts": time.time(), "from": frm,
                   "to": to, "kind": kind, "text": text}
            self._append(msg)
            if kind != "system" and not self.compact_pending and self.needs_compaction():
                self.compact_pending = True
            self.cond.notify_all()
        await self.broadcast_gui({"type": "message", "message": msg})
        await self.broadcast_gui({"type": "state", "state": self.snapshot_light()})
        return msg

    def snapshot_light(self):
        s = self.snapshot()
        s.pop("messages")
        return s

    # ---------- ops ----------
    async def op_hello(self, req):
        agent = req["agent"]
        self.touch(agent)
        # A fresh agent starts reading from the current compaction point, not from the beginning.
        self.cursors.setdefault(agent, self.summary_upto)
        self._save_state()
        await self.broadcast_gui({"type": "state", "state": self.snapshot_light()})
        return {"ok": True, "agent": agent, "context": self.context_for(agent)}

    async def op_send(self, req):
        agent = req["agent"]
        self.touch(agent)
        text = req.get("text", "")
        if not text.strip():
            return {"ok": False, "error": "empty message"}
        kind = req.get("kind", "msg")
        if kind not in ("msg", "done"):
            kind = "msg"
        msg = await self.add_message(agent, req.get("to") or "other", text, kind)
        return {"ok": True, "id": msg["id"]}

    async def op_wait(self, req):
        agent = req["agent"]
        timeout = max(1.0, min(float(req.get("timeout", 240)), 3600))
        self.cursors.setdefault(agent, self.summary_upto)
        self.touch(agent, waiting=True)
        await self.broadcast_gui({"type": "state", "state": self.snapshot_light()})
        deadline = time.monotonic() + timeout

        def pending():
            cur = self.cursors.get(agent, 0)
            return [m for m in self.messages if m["id"] > cur and self.is_for(m, agent)]

        try:
            async with self.cond:
                while not pending() and not self.stopped:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        await asyncio.wait_for(self.cond.wait(), remaining)
                    except asyncio.TimeoutError:
                        break
                msgs = pending()
                if msgs:
                    self.cursors[agent] = msgs[-1]["id"]
                    self._save_state()
        finally:
            self.touch(agent, waiting=False)
            await self.broadcast_gui({"type": "state", "state": self.snapshot_light()})

        resp = {"ok": True, "messages": [self.for_delivery(m) for m in msgs],
                "stopped": self.stopped}
        if self.compact_pending and msgs:
            resp["compact_requested"] = True
        return resp

    def context_for(self, agent):
        live = self.live_messages()[-RECENT_IN_CONTEXT:]
        return {
            "task": self.task,
            "summary": self.summary,
            "recent": [self.for_delivery(m) for m in live],
            "compact_requested": self.compact_pending,
            "stopped": self.stopped,
        }

    async def op_context(self, req):
        self.touch(req["agent"])
        return {"ok": True, **self.context_for(req["agent"])}

    async def op_task(self, req):
        agent = req["agent"]
        self.touch(agent)
        self.task = req["text"]
        self.stopped = False
        self._save_state()
        await self.add_message(agent, "all", f"TASK SET: {self.task}", "system")
        return {"ok": True}

    async def op_compact(self, req):
        agent = req["agent"]
        self.touch(agent)
        summary = req.get("text", "").strip()
        if not summary:
            return {"ok": False, "error": "empty summary"}
        upto = self.messages[-1]["id"] if self.messages else 0
        self.summary = summary
        self.summary_upto = upto
        self.compact_pending = False
        self._save_state()
        # The summary message itself lands after the floor, so the other agent receives it.
        await self.add_message(agent, "all", summary, "summary")
        return {"ok": True, "compacted_upto": upto}

    async def op_get(self, req):
        mid = int(req["id"])
        for m in self.messages:
            if m["id"] == mid:
                return {"ok": True, "message": m}
        return {"ok": False, "error": f"no message {mid}"}

    async def op_status(self, req):
        return {"ok": True, **self.snapshot_light()}

    async def op_stop(self, req, who="user"):
        self.stopped = True
        self._save_state()
        await self.add_message(who, "all", "SESSION STOPPED. Finish your current step and stop.", "system")
        async with self.cond:
            self.cond.notify_all()
        return {"ok": True}

    async def op_reset(self, req, who="user"):
        archive = self.data_dir / f"transcript-{time.time_ns()}.jsonl"
        if self.log_path.exists():
            self.log_path.rename(archive)
        self.messages = []
        self.task = self.summary = ""
        self.summary_upto = 0
        self.compact_pending = False
        self.cursors = {}
        self.stopped = False
        self._save_state()
        await self.broadcast_gui({"type": "reset", "state": self.snapshot()})
        return {"ok": True, "archived_to": str(archive)}

    OPS = {"hello", "send", "wait", "context", "task", "compact", "get", "status", "stop", "reset"}

    async def dispatch(self, req):
        if not isinstance(req, dict):
            return {"ok": False, "error": "request must be a JSON object"}
        op = req.get("op")
        if op not in self.OPS:
            return {"ok": False, "error": f"unknown op {op!r}"}
        if op not in ("get", "status", "stop", "reset") and not req.get("agent"):
            return {"ok": False, "error": "missing 'agent'"}
        if "agent" in req and (not isinstance(req["agent"], str) or len(req["agent"]) > 80):
            return {"ok": False, "error": "agent must be a string of at most 80 characters"}
        if "text" in req and not isinstance(req["text"], str):
            return {"ok": False, "error": "text must be a string"}
        if "to" in req and not isinstance(req["to"], str):
            return {"ok": False, "error": "recipient must be a string"}
        return await getattr(self, f"op_{op}")(req)


# ---------- TCP server ----------
async def handle_tcp(bridge: Bridge, reader, writer):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line.decode("utf-8"))
                resp = await bridge.dispatch(req)
            except Exception as e:  # malformed request should not kill the server
                resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            writer.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, ValueError):
        pass
    finally:
        writer.close()


# ---------- GUI (minimal HTTP + server-sent events) ----------
async def handle_http(bridge: Bridge, reader, writer):
    try:
        request_line = (await asyncio.wait_for(reader.readline(), 10)).decode("latin-1").strip()
        if not request_line:
            return
        method, path, _ = request_line.split(" ", 2)
        headers = {}
        header_bytes = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), 10)
            if not line:
                return
            if line in (b"\r\n", b"\n"):
                break
            header_bytes += len(line)
            if header_bytes > 16384:
                return
            key, value = line.decode("latin-1").split(":", 1)
            headers[key.lower()] = value.strip()

        def respond(status, ctype, body: bytes):
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\n"
                         f"Content-Length: {len(body)}\r\nCache-Control: no-store\r\n"
                         f"X-Content-Type-Options: nosniff\r\nX-Frame-Options: DENY\r\n"
                         f"Connection: close\r\n\r\n".encode() + body)

        address = writer.get_extra_info("sockname")
        allowed_hosts = {f"127.0.0.1:{address[1]}", f"localhost:{address[1]}", f"[::1]:{address[1]}"}
        host = headers.get("host", "").lower()
        origin = headers.get("origin")
        parsed_origin = urlsplit(origin) if origin else None
        if host not in allowed_hosts or (parsed_origin and (parsed_origin.scheme != "http" or parsed_origin.netloc.lower() != host)):
            respond("403 Forbidden", "text/plain", b"local same-origin requests only")
        elif method == "POST" and headers.get("content-type", "").split(";", 1)[0] != "application/json":
            respond("415 Unsupported Media Type", "text/plain", b"use application/json")
        elif method == "GET" and path in ("/", "/index.html"):
            respond("200 OK", "text/html; charset=utf-8", (HERE / "gui.html").read_bytes())
        elif method == "GET" and path == "/api/state":
            respond("200 OK", "application/json", json.dumps(bridge.snapshot()).encode())
        elif method == "POST" and path in ("/api/stop", "/api/reset"):
            op = path.rsplit("/", 1)[1]
            result = await getattr(bridge, f"op_{op}")({})
            respond("200 OK", "application/json", json.dumps(result).encode())
        elif method == "GET" and path == "/events":
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                         b"Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n")
            q = asyncio.Queue(maxsize=1000)
            bridge.gui_queues.add(q)
            try:
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                        writer.write(f"data: {json.dumps(ev)}\n\n".encode())
                    except asyncio.TimeoutError:
                        # periodic refresh keeps online/offline status accurate
                        writer.write(f"data: {json.dumps({'type': 'state', 'state': bridge.snapshot_light()})}\n\n".encode())
                    await writer.drain()
            finally:
                bridge.gui_queues.discard(q)
        else:
            respond("404 Not Found", "text/plain", b"not found")
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError, ValueError, asyncio.TimeoutError):
        pass
    finally:
        writer.close()


async def main():
    ap = argparse.ArgumentParser(description="Claude <-> Codex bridge server")
    ap.add_argument("--host", default=os.environ.get("BRIDGE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("BRIDGE_PORT", 8765)))
    ap.add_argument("--gui-port", type=int, default=int(os.environ.get("BRIDGE_GUI_PORT", 8766)))
    ap.add_argument("--data", default=str(HERE / "bridge_data"))
    args = ap.parse_args()
    if args.host != "localhost":
        try:
            if not ipaddress.ip_address(args.host).is_loopback:
                ap.error("--host must be a loopback address; this bridge has no remote authentication")
        except ValueError:
            ap.error("--host must be localhost or a numeric loopback address")

    bridge = Bridge(Path(args.data))
    tcp = await asyncio.start_server(lambda r, w: handle_tcp(bridge, r, w), args.host, args.port, limit=MAX_REQUEST_BYTES)
    gui = await asyncio.start_server(lambda r, w: handle_http(bridge, r, w), args.host, args.gui_port)
    print(f"Bridge TCP  : {args.host}:{args.port}")
    print(f"Bridge GUI  : http://{args.host}:{args.gui_port}/")
    print(f"Transcript  : {bridge.log_path}")
    async with tcp, gui:
        await asyncio.gather(tcp.serve_forever(), gui.serve_forever())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
