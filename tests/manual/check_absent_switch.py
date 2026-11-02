#!/usr/bin/python
"""Bounded integration test for the first-boot absent-switch path.

Run as the active desktop user with the package already installed.  It asks
for one administrator authorization, briefly removes the /dev node of the
Intel HID switch (simulating the state before the first fold), verifies that a
software tent mode is still applied, restores the node, and returns to
automatic mode.

The sysfs device and the running display server are not unbound; only the
device node is removed and recreated with its original major/minor pair.

Safety: the marker directory and log file are created by this process with
``tempfile.mkdtemp()``/``tempfile.mkstemp()`` (mode 0700/0600, owned by the
invoking user) and passed to the root helper, which verifies that the directory
is a real directory, not a symlink, owned by the expected uid with mode 0700.
Nothing is ever chmod 0777 or read from a predictable /tmp path.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gi.repository import Gio, GLib

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = REPO_ROOT / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from panasonic_posture.protocol import IFACE, NAME, PATH  # noqa: E402

HELPER = REPO_ROOT / "tests" / "manual" / "absent_switch_root.sh"
SERVICE = "panasonic-posture-manager.service"

BUS = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)


def call(method, parameters=None):
    return BUS.call_sync(NAME, PATH, IFACE, method, parameters, None,
                         Gio.DBusCallFlags.NO_AUTO_START, 3000, None)


def state():
    return json.loads(call("GetState").unpack()[0])


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = state()
        except (GLib.Error, ValueError):
            time.sleep(0.25)
            continue
        if last.get("error_code"):
            raise RuntimeError(f"{last['error_code']}: {last.get('error_detail')}")
        if predicate(last):
            return last
        time.sleep(0.25)
    raise RuntimeError(f"timeout waiting for {description}: {last}")


def set_mode(mode):
    call("SetMode", GLib.Variant("(s)", (mode,)))


def marker(markers: Path, name: str) -> bool:
    return (markers / name).exists()


def restart_agent():
    subprocess.run(
        ["/usr/bin/systemctl", "--user", "restart",
         "panasonic-posture-manager-session.service"],
        check=False,
        timeout=20,
    )
    time.sleep(1)


def main() -> int:
    if not HELPER.is_file():
        raise SystemExit(f"missing root helper: {HELPER}")
    # Private, predictable-only-to-us directory; the root helper verifies it.
    markers = Path(tempfile.mkdtemp(prefix="posture-absent-test-"))
    log_fd, log_name = tempfile.mkstemp(prefix="posture-absent-test-", suffix=".log")
    os.close(log_fd)
    log_path = Path(log_name)
    command = "set -e; exec /bin/bash {} {} {}".format(
        shlex.quote(str(HELPER)),
        shlex.quote(str(markers)),
        os.getuid(),
    )
    log = log_path.open("w")
    root = subprocess.Popen(
        ["/usr/bin/pkexec", "/bin/bash", "-c", command],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    result = 0
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not marker(markers, "ready"):
            if root.poll() is not None:
                raise RuntimeError("administrator helper exited before ready")
            time.sleep(0.25)
        if not marker(markers, "ready"):
            raise RuntimeError(
                "administrator helper did not start (authorization timed out?)"
            )

        # The package service is already running.  Restart the user adapter so
        # the test exercises the installed session code, then wait for both
        # sides to exchange a lease.
        restart_agent()
        initial = wait_for(
            lambda s: s.get("session_connected") and s.get("applied")
            and s["applied"].get("mode") == "laptop",
            20,
            "laptop mode after package install",
        )
        print("initial:", initial["applied"]["mode"], "session:",
              initial["session_connected"], flush=True)

        set_mode("tent")
        tent = wait_for(
            lambda s: s.get("applied") and s["applied"].get("mode") == "tent",
            10,
            "software tent before removal",
        )
        print("before removal: tent applied, hardware status",
              tent["hardware_status"], flush=True)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not (
                marker(markers, "removed") or marker(markers, "skipped")):
            if root.poll() is not None:
                break
            time.sleep(0.2)
        if marker(markers, "skipped"):
            print("SKIP: switch node was already absent; verifying virtual-only tent",
                  flush=True)
        elif not marker(markers, "removed"):
            raise RuntimeError("root helper did not remove the switch node")
        else:
            absent = wait_for(
                lambda s: s.get("hardware_status") == "absent"
                and s.get("applied") and s["applied"].get("mode") == "tent",
                6,
                "absent hardware with tent still applied",
            )
            print("absent: hardware", absent["hardware"], absent["hardware_status"],
                  "applied", absent["applied"]["mode"],
                  "error_code", absent["error_code"], flush=True)

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not marker(markers, "restored"):
            if root.poll() is not None:
                break
            time.sleep(0.2)
        if not marker(markers, "restored") and not marker(markers, "skipped"):
            raise RuntimeError(
                "root helper did not restore the switch node; see "
                f"{log_path}"
            )
        if not marker(markers, "skipped"):
            restored = wait_for(
                lambda s: s.get("hardware_status") == "ok" and s.get("applied")
                and s["applied"].get("mode") == "tent",
                8,
                "hardware status restored with tent applied",
            )
            print("restored: hardware status", restored["hardware_status"],
                  "applied", restored["applied"]["mode"], flush=True)

        set_mode("auto")
        final = wait_for(
            lambda s: s.get("applied") and s["applied"].get("mode") == "laptop",
            10,
            "automatic laptop restore",
        )
        print("final:", final["applied"]["mode"], "manual", final["manual"],
              flush=True)
        root.wait(timeout=20)
        if root.returncode not in (0, None):
            raise RuntimeError(f"administrator helper exited {root.returncode}")
        print("ABSENT-SWITCH INTEGRATION PASS")
    except Exception as error:  # noqa: BLE001 - report and restore in finally
        result = 1
        print("FAIL:", error, flush=True)
        try:
            set_mode("auto")
        except GLib.Error:
            pass
    finally:
        log.close()
        if root.poll() is None:
            root.wait(timeout=30)
        if log_path.is_file():
            print("root log:", flush=True)
            print(log_path.read_text(errors="replace")[-2000:], flush=True)
        shutil.rmtree(markers, ignore_errors=True)
        log_path.unlink(missing_ok=True)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
