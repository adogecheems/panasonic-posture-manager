#!/usr/bin/python
"""Interactive fault-injection integration test (requires polkit authorization).

The package must already be installed and the session adapter running::

    /usr/bin/python tests/manual/check_faults.py

The test kills the privileged relay and then the daemon itself, and verifies
that the service reports a stable error code and returns to automatic laptop
mode (for the daemon case systemd restarts it).
"""

import json
import shlex
import subprocess
import sys
import time
from pathlib import Path

from gi.repository import Gio, GLib

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = REPO_ROOT / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from panasonic_posture.protocol import IFACE, NAME, PATH  # noqa: E402

BUS = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def call(method, parameters=None):
    return BUS.call_sync(NAME, PATH, IFACE, method, parameters, None,
                         Gio.DBusCallFlags.NO_AUTO_START, 4000, None)


def state():
    return json.loads(call("GetState").unpack()[0])


def choose(mode):
    call("SetMode", GLib.Variant("(s)", (mode,)))


def wait_for(predicate, description, timeout=8.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = state()
        except GLib.Error:
            time.sleep(0.15)
            continue
        if predicate(last):
            return last
        time.sleep(0.15)
    raise RuntimeError(f"state timeout waiting for {description}: {last}")


def main() -> int:
    helper_script = Path(__file__).with_name("fault_signal.py")
    command = "exec /usr/bin/python -I " + shlex.quote(str(helper_script))
    helper = subprocess.Popen(
        ["/usr/bin/pkexec", "/bin/bash", "-c", command],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    line = helper.stdout.readline().strip()
    if line != "READY":
        raise SystemExit("authorization or fault helper failed: " + line)
    try:
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "restart",
             "panasonic-posture-manager-session.service"],
            check=False,
            timeout=20,
        )
        wait_for(
            lambda s: s.get("session_connected") and s.get("applied")
            and s["applied"].get("mode") == "laptop",
            "session connected in laptop mode",
            timeout=20,
        )
        guard = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("manual_reset_guard.py"))],
            stdin=subprocess.PIPE,
        )
        try:
            for action in ("backend", "daemon"):
                choose("tent")
                wait_for(
                    lambda s: s.get("applied") and s["applied"].get("mode") == "tent",
                    "tent mode applied",
                )
                helper.stdin.write(action + "\n")
                helper.stdin.flush()
                killed = helper.stdout.readline().strip()
                if killed != "KILLED " + action:
                    raise RuntimeError(f"unexpected helper reply: {killed!r}")
                if action == "backend":
                    current = wait_for(
                        lambda s: s.get("error_code") and s.get("applied") is None,
                        "relay error state",
                    )
                    assert current["error_code"].startswith("relay-exited"), current
                    choose("auto")
                wait_for(
                    lambda s: s.get("applied") and s["applied"].get("mode") == "laptop",
                    f"laptop recovery after {action} kill",
                    timeout=15 if action == "daemon" else 8,
                )
                print(action + " fault recovered to laptop mode", flush=True)
        finally:
            try:
                choose("auto")
            finally:
                try:
                    guard.stdin.write(b"c")
                    guard.stdin.flush()
                    guard.stdin.close()
                except OSError:
                    pass
                guard.wait(timeout=5)
    finally:
        try:
            helper.stdin.close()
        except OSError:
            pass
        helper.wait(timeout=5)
    print("Fault checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
