#!/usr/bin/python
"""Bounded explicit integration run against the installed system service.

Temporarily switches the real modes and restores automatic mode in a guard
process if this script dies.  Run as the active desktop user on a stationary
normal laptop with the package installed::

    /usr/bin/python tests/manual/check_live.py
"""

import json
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
                         Gio.DBusCallFlags.NO_AUTO_START, 3000, None)


def state():
    return json.loads(call("GetState").unpack()[0])


def wait_for(predicate, description, timeout=7.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = state()
        except (GLib.Error, ValueError):
            time.sleep(0.15)
            continue
        if last.get("error_code"):
            raise RuntimeError(f"{last['error_code']}: {last.get('error_detail')}")
        if predicate(last):
            return last
        time.sleep(0.15)
    raise RuntimeError(f"timed out waiting for {description}: {last}")


def mode(value):
    call("SetMode", GLib.Variant("(s)", (value,)))
    expected = "laptop" if value == "auto" else value
    current = wait_for(
        lambda s: s.get("manual") == value and s.get("applied")
        and s["applied"].get("mode") == expected,
        f"{value} applied",
    )
    print("Applied:", expected, flush=True)
    return current


def main():
    initial = state()
    if not initial.get("session_connected"):
        raise SystemExit(
            "session adapter is not connected; log out/in once or run "
            "systemctl --user start panasonic-posture-manager-session.service"
        )
    if initial.get("hardware") is not False or initial.get("pose") != "laptop":
        raise SystemExit(
            "run this test on a stationary normal laptop (hardware switch off, "
            f"pose laptop): {initial}"
        )
    guard = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("manual_reset_guard.py"))],
        stdin=subprocess.PIPE,
    )
    try:
        for value in ("tent", "stand", "tablet", "auto"):
            current = mode(value)
            if value != "auto":
                assert current["applied"]["tablet"] is True, current
                assert current["applied"]["disable_internal_input"] is True, current
            time.sleep(0.5)
    finally:
        try:
            mode("auto")
        except (GLib.Error, RuntimeError) as error:
            print("WARNING: could not restore automatic mode:", error, flush=True)
        try:
            guard.stdin.write(b"c")
            guard.stdin.flush()
            guard.stdin.close()
        except OSError:
            pass
        guard.wait(timeout=5)
    print("Live service transactions passed; automatic mode restored.")


if __name__ == "__main__":
    main()
