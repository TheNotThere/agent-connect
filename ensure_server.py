"""Make sure the agent bridge server is running, starting it if needed.

Safe to run any time, from any agent. If the server is already up this changes
nothing. Otherwise it starts bridge_server.py as a separate background process
that keeps running after the calling agent exits. Output goes to server.log.

Exit codes: 0 server is up, 1 server could not be started.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WAIT_SECONDS = 10
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
GUI_PORT = int(os.environ.get("BRIDGE_GUI_PORT", 8766))
GUI_URL = f"http://{'[' + HOST + ']' if ':' in HOST else HOST}:{GUI_PORT}/"


def server_is_up():
    try:
        result = subprocess.run(
            [sys.executable, str(HERE / "bridge.py"), "status"],
            cwd=HERE, capture_output=True, timeout=3,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def start_server():
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = str(pythonw) if pythonw.exists() else sys.executable
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with (HERE / "server.log").open("a", encoding="utf-8") as log:
        def launch(creationflags):
            subprocess.Popen(
                [interpreter, "-u", str(HERE / "bridge_server.py")],
                cwd=HERE,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=creationflags,
                start_new_session=sys.platform != "win32",
            )

        if sys.platform != "win32":
            launch(0)
            return
        try:
            # Leaving the caller's job lets the server outlive the agent's shell.
            launch(flags | subprocess.CREATE_BREAKAWAY_FROM_JOB)
        except PermissionError:
            # Some agent shells forbid leaving their job.
            launch(flags)


def main():
    if server_is_up():
        print(f"bridge: server already running. GUI: {GUI_URL}")
        return 0
    start_server()
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if server_is_up():
            print(f"bridge: server started. GUI: {GUI_URL}")
            return 0
    print(f"bridge: server did not start. See {HERE / 'server.log'}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
