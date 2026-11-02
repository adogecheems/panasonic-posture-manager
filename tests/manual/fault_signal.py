#!/usr/bin/python
"""Explicit, bounded fault injection for the installed service (runs as root).

The helper is launched through pkexec by ``check_faults.py``.  It only kills
the PID it can positively identify as this project's daemon (or the single
``relay`` child of that daemon), and only twice per authorization.
"""

import os
import select
import signal
import subprocess
import sys
from pathlib import Path

DAEMON_MODULE = "panasonic_posture.service.daemon"
RELAY_BASENAME = "relay"


def read_cmdline(pid: int) -> list[bytes]:
    return [
        part
        for part in Path("/proc", str(pid), "cmdline").read_bytes().split(b"\0")
        if part
    ]


def is_daemon_process(pid: int) -> bool:
    try:
        args = read_cmdline(pid)
    except FileNotFoundError:
        return False
    for arg in args:
        text = arg.decode(errors="replace")
        if text == DAEMON_MODULE or text.endswith("/panasonic_posture/service/daemon.py"):
            return True
        if text == "panasonic_posture.service.daemon":
            return True
    return False


def relay_children(pid: int) -> list[int]:
    try:
        children = Path("/proc", str(pid), "task", str(pid), "children").read_text().split()
    except OSError:
        return []
    found = []
    for child in children:
        try:
            args = read_cmdline(int(child))
        except (FileNotFoundError, ValueError):
            continue
        if args and Path(args[0].decode(errors="replace")).name == RELAY_BASENAME:
            found.append(int(child))
    return found


def main() -> int:
    if os.geteuid() != 0:
        raise SystemExit("root required")
    print("READY", flush=True)
    for _ in range(2):
        ready, _, _ = select.select([sys.stdin], [], [], 40)
        if not ready:
            break
        action = sys.stdin.readline().strip()
        if action not in ("backend", "daemon"):
            break
        output = subprocess.check_output(
            ["/usr/bin/systemctl", "show", "panasonic-posture-manager.service",
             "--property=MainPID", "--value"],
            text=True,
        )
        pid = int(output.strip() or "0")
        if pid <= 1:
            raise RuntimeError("service not running")
        if not is_daemon_process(pid):
            raise RuntimeError("daemon identity mismatch")
        if action == "backend":
            found = relay_children(pid)
            if len(found) != 1:
                raise RuntimeError(f"relay identity not unique: {found}")
            pid = found[0]
        os.kill(pid, signal.SIGKILL)
        print("KILLED " + action, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
