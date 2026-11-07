"""Agent bridge client. Used by Claude Code and Codex from their shell tools.

Examples (agent name from --as or the BRIDGE_AGENT env var):
  python bridge.py join  --as codex
  python bridge.py task  --as codex "Build a CLI todo app in ./todo"
  python bridge.py talk  --as codex "I wrote todo.py, please review it"   # send + wait for reply
  python bridge.py send  --as claude --file reply.md
  python bridge.py wait  --as claude --timeout 300
  python bridge.py send  --as claude --done "All tests pass, finished."
  python bridge.py compact --as claude "Summary: ..."
  python bridge.py context --as claude
  python bridge.py get 12
  python bridge.py status

Message text can be given inline, with --file PATH, or as "-" to read stdin.
Exit codes: 0 ok, 2 wait timed out with nothing new, 3 session stopped, 1 error.
"""

import argparse
import json
import os
import socket
import sys
import time

HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", 8765))
MAX_RESPONSE_BYTES = 16 * 1024 * 1024

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass


def call(req, timeout=30):
    try:
        with socket.create_connection((HOST, PORT), timeout=10) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(req) + "\n").encode("utf-8"))
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    sys.exit("bridge: server response is too large")
    except OSError as e:
        sys.exit(f"bridge: cannot reach server at {HOST}:{PORT} ({e}). "
                 f"Start it with: python bridge_server.py")
    try:
        resp = json.loads(buf.decode("utf-8"))
    except (ValueError, UnicodeError):
        sys.exit("bridge: server returned an invalid JSON response")
    if not resp.get("ok"):
        sys.exit(f"bridge error: {resp.get('error')}")
    return resp


def fmt_time(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts))


def print_message(m):
    label = {"done": " [DONE]", "summary": " [SUMMARY]", "system": " [SYSTEM]"}.get(m["kind"], "")
    print(f"--- #{m['id']} {m['from']} -> {m['to']} @ {fmt_time(m['ts'])}{label} ---")
    print(m["text"])
    print()


def print_context(ctx):
    print(f"TASK: {ctx.get('task') or '(none set)'}")
    if ctx.get("summary"):
        print("\nSUMMARY OF EARLIER CONVERSATION:")
        print(ctx["summary"])
    recent = ctx.get("recent") or []
    if recent:
        print(f"\nRECENT MESSAGES ({len(recent)}):\n")
        for m in recent:
            print_message(m)
    compact_hint(ctx)


def compact_hint(resp):
    if resp.get("compact_requested"):
        print(">>> COMPACTION REQUESTED: the shared history is getting long. Before your next "
              "message, post a short summary (goal, decisions, current state, open items) with:\n"
              ">>>   python bridge.py compact --as <you> \"<summary>\"\n")
    if resp.get("stopped"):
        print(">>> SESSION STOPPED by the user. Wrap up and do not wait for more messages.")


def read_text(args):
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            return f.read()
    text = " ".join(args.text or [])
    if text == "-":
        return sys.stdin.read()
    if not text.strip():
        sys.exit("bridge: no message text given (inline, --file PATH, or - for stdin)")
    return text


def do_wait(agent, timeout):
    resp = call({"op": "wait", "agent": agent, "timeout": timeout}, timeout=timeout + 15)
    msgs = resp["messages"]
    for m in msgs:
        print_message(m)
    compact_hint(resp)
    if resp.get("stopped") and not msgs:
        return 3
    if not msgs:
        print(f"(no new messages after {timeout:.0f}s; run wait again)")
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description="Claude <-> Codex bridge client")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, text=False, **kw):
        p = sub.add_parser(name, **kw)
        p.add_argument("--as", dest="agent", default=os.environ.get("BRIDGE_AGENT"))
        if text:
            p.add_argument("text", nargs="*")
            p.add_argument("--file")
        return p

    add("join", help="register and print current context")
    p = add("send", text=True, help="send a message")
    p.add_argument("--to", default="other")
    p.add_argument("--done", action="store_true", help="mark the task as finished from your side")
    p = add("wait", help="block until a message arrives")
    p.add_argument("--timeout", type=float, default=240)
    p = add("talk", text=True, help="send a message, then wait for the reply")
    p.add_argument("--to", default="other")
    p.add_argument("--timeout", type=float, default=240)
    add("task", text=True, help="set the shared task")
    add("compact", text=True, help="post a summary that replaces older history")
    add("context", help="print task, summary and recent messages")
    p = sub.add_parser("get", help="print one full message")
    p.add_argument("id", type=int)
    sub.add_parser("status", help="server status")
    sub.add_parser("stop", help="stop the session")

    args = ap.parse_args()
    needs_agent = args.cmd not in ("get", "status", "stop")
    if needs_agent and not args.agent:
        sys.exit("bridge: pass --as <name> (e.g. claude or codex) or set BRIDGE_AGENT")
    agent = getattr(args, "agent", None)

    if args.cmd == "join":
        print(f"Joined bridge as '{agent}'.\n")
        print_context(call({"op": "hello", "agent": agent})["context"])
    elif args.cmd == "send":
        r = call({"op": "send", "agent": agent, "to": args.to, "text": read_text(args),
                  "kind": "done" if args.done else "msg"})
        print(f"sent #{r['id']}")
    elif args.cmd == "wait":
        return do_wait(agent, args.timeout)
    elif args.cmd == "talk":
        r = call({"op": "send", "agent": agent, "to": args.to, "text": read_text(args)})
        print(f"sent #{r['id']}, waiting for reply...\n")
        return do_wait(agent, args.timeout)
    elif args.cmd == "task":
        call({"op": "task", "agent": agent, "text": read_text(args)})
        print("task set")
    elif args.cmd == "compact":
        r = call({"op": "compact", "agent": agent, "text": read_text(args)})
        print(f"history compacted up to #{r['compacted_upto']}")
    elif args.cmd == "context":
        print_context(call({"op": "context", "agent": agent}))
    elif args.cmd == "get":
        print_message(call({"op": "get", "id": args.id})["message"])
    elif args.cmd == "status":
        r = call({"op": "status"})
        print(json.dumps({k: r[k] for k in ("task", "stopped", "agents", "live_count", "live_chars")}, indent=2))
    elif args.cmd == "stop":
        call({"op": "stop"})
        print("session stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
