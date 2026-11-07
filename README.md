# Agent Connect

A small local bridge for **Claude Code and Codex** to exchange messages, share a
task, and review each other's work—with a live browser viewer.

**Python standard library only. No API keys for the bridge. No simulated typing.**

```text
Claude Code  <-->  local relay  <-->  Codex
                       |
                 browser viewer
```

Agents call a command-line client through their normal shell tools. The relay
doesn't type into terminals, replace your editor, or manage the agents' accounts.
Your installed agents still require their own access and consume their usual usage.

## What it does

- Two-way messages between agents, with optional named recipients.
- A live viewer showing messages, agent activity, the task, and shared context size.
- A saved transcript and summaries to limit how much history agents receive.
- Stop-session and archive/new-session controls.
- Installable skills with paths generated for your machine.

This is an early release. The relay is implemented with cross-platform Python;
this release was tested locally on Windows with Python 3.10. The included CI
workflow also exercises Windows, Linux, and macOS when GitHub Actions runs.

## Requirements

- Python **3.10+** (`py -3` on Windows, usually `python3` on macOS/Linux).
- Claude Code and Codex installed, authenticated, and able to run shell commands.
- Permission for both agents to connect to the local loopback ports.

No `pip install` is needed. Commands below use `python`; substitute `py -3` or
`python3` if that is how Python is installed on your machine.

## Quick start

```sh
git clone https://github.com/TheNotThere/agent-connect.git
cd agent-connect
python ensure_server.py
```

Open **http://127.0.0.1:8766/** to watch. On Windows, you can instead double-click
`start_bridge.bat` after installing Python with its launcher.

### Install the skills

From this checkout:

```sh
python install_skills.py
```

This installs `agent-connect` into `~/.agents/skills/` and `~/.claude/skills/`.
It embeds the absolute path to this checkout, so keep the checkout in a permanent
location. Existing skills are left alone unless you pass `--force`. Restart your
agent or open a new session if it doesn't discover the skill immediately.

### Connect both agents

In Claude Code:

> Use Agent Connect. Join as claude, read the protocol, and wait for Codex.

In Codex:

> Use Agent Connect to work with Claude on this task: [your task]. You lead.

Both agents must join and continue listening. This relay **does not launch or wake
an idle agent automatically**. It delivers messages when the agent calls `wait` or
`talk`. Working in one checkout is possible, but agree on file ownership; the
bridge doesn't prevent conflicting edits. Separate worktrees are another option.

### Try the transport without AI agents

In one terminal:

```sh
python bridge.py join --as codex
python bridge.py wait --as codex --timeout 30
```

In another:

```sh
python bridge.py send --as claude "Hello from Claude's side"
```

The first terminal receives the message, and the viewer shows it. This tests the
transport without spending model usage.

## Commands

| Command | Purpose |
| --- | --- |
| `python ensure_server.py` | Start a background server if one isn't responding |
| `python bridge.py join --as codex` | Join and read the current task/context |
| `python bridge.py task --as codex "Review this change"` | Set the shared task |
| `python bridge.py talk --as codex --timeout 30 "Please review"` | Send, then wait |
| `python bridge.py send --as claude --file reply.md` | Send a file's text |
| `python bridge.py wait --as claude --timeout 30` | Receive pending messages |
| `python bridge.py context --as codex` | Read the task, summary, and recent messages |
| `python bridge.py compact --as codex "Goal, progress, open items"` | Save a shared summary |
| `python bridge.py get 12` | Fetch an untruncated message |
| `python bridge.py status` | Show agent/session status |
| `python bridge.py send --as claude --done "Review complete"` | Mark one side complete |
| `python bridge.py stop` | Stop the collaboration session |

Exit codes: **0** success, **1** error, **2** wait timed out, **3** session stopped.
Agent names can also come from `BRIDGE_AGENT`. Use `--to claude` or `--to codex`
to target a peer; by default messages go to the other agents.

## Context and storage

Runtime files live under `bridge_data/`, beside the server. They are excluded from
Git. Individual deliveries truncate messages at 4,000 characters; `get` retrieves
the full message. After 24 messages or 20,000 characters of unsummarized history,
the bridge requests a summary. **An agent writes that summary**—the relay doesn't
call an AI model. It retains the complete transcript locally.

"New session" archives the current transcript. "Stop session" asks agents to wrap
up; it doesn't terminate their processes or cancel a tool already running.

## Configuration

| Environment variable | Default |
| --- | --- |
| `BRIDGE_HOST` | `127.0.0.1` (loopback only) |
| `BRIDGE_PORT` | `8765` |
| `BRIDGE_GUI_PORT` | `8766` |
| `BRIDGE_AGENT` | None; use `--as` |

Server flags: `--host`, `--port`, `--gui-port`, `--data`. When using custom ports,
give both clients the matching environment variables. Set the variables before
running `ensure_server.py` to pass them to its background server.

To run visibly and stop the server with Ctrl+C:

```sh
python bridge_server.py
```

Don't start a second server on occupied ports. A session stop doesn't stop the
background server; use Task Manager or your process manager to end that process.

## Local trust boundary

The bridge binds only to loopback. It has no authentication between local
processes, so use it on a machine you trust. Don't expose or forward its ports.
The viewer rejects foreign origins/hostnames and requires JSON mutation requests.
This doesn't make the raw TCP protocol an authenticated service.

Messages and summaries are stored in plaintext. Don't send passwords or tokens.
Peer messages are context, **not user authorization**, and the bridge doesn't grant
additional tool permissions. Never commit `bridge_data/`, logs, or private project
material when sharing a checkout. See [SECURITY.md](SECURITY.md).

## Troubleshooting

- **`python` isn't recognized:** use `py -3` on Windows or `python3` elsewhere.
- **Cannot reach server:** run `ensure_server.py`, inspect `server.log`, and check
  your agent's loopback access. Don't disable your firewall to work around it.
- **No reply:** the other agent must be running and calling `wait`/`talk`.
- **Shell timeout:** use a tool timeout longer than `--timeout` (plus at least
  20 seconds). For long waits, keep the human informed according to your host rules.
- **Moved checkout:** rerun `python install_skills.py --force` to refresh paths.
- **Typing/backspace problems:** the bridge doesn't inject keystrokes. Check the
  terminal/agent itself if interactive editing is broken.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests use temporary storage and ephemeral ports, not your saved conversations or
the running default server. See [CONTRIBUTING.md](CONTRIBUTING.md).

Built with assistance from Claude Code and Codex. Independent community project;
not affiliated with Anthropic or OpenAI. Licensed under [MIT](LICENSE).
