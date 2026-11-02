#!/usr/bin/python
"""Session-bus D-Bus integration checks for the 0.3.0 contract.

Run by ``make check`` inside ``dbus-run-session``:

    dbus-run-session -- env PYTHONPATH=src python tests/integration/check_dbus.py

The real daemon runs on the *session* bus (``--session-bus``), so logind/polkit
authorization and the privileged relay are skipped.  This test covers the
public wire contract: state schema v2, mode selection, persisted tent prompt,
D-Bus error codes, the ``Retry`` method and the 0.3.0 ``SessionUpdate(u b s s)``
lease handshake.  The test also owns the valid per-uid adapter name
``work.mmoe.panasonic.Posture.Session.u<uid>`` so the negotiated identity is
exercised (the session-bus development daemon intentionally bypasses the
system-bus authorization checks).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gi.repository import Gio, GLib

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from panasonic_posture.protocol import (  # noqa: E402  (after sys.path setup)
    NAME,
    PATH,
    STATE_VERSION,
    session_name,
)

BUS_TYPE = Gio.BusType.SESSION
DAEMON = Path(__file__).with_name("session_daemon.py")


def wait_until(predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except GLib.Error as error:
            last_error = error
        time.sleep(0.05)
    raise RuntimeError(f"timeout waiting for {description}: {last_error}")


def main() -> int:
    if not DAEMON.is_file():
        raise SystemExit(f"missing helper: {DAEMON}")
    adapter_name = session_name(os.getuid())
    assert Gio.dbus_is_name(adapter_name), adapter_name
    with tempfile.TemporaryDirectory(prefix="posture-dbus-") as config_dir:
        config_path = Path(config_dir) / "settings.json"
        process = subprocess.Popen(
            [sys.executable, str(DAEMON), str(config_path)],
            cwd=str(REPO_ROOT),
        )
        connection = None
        owner = None
        try:
            connection = Gio.bus_get_sync(BUS_TYPE, None)
            owner = Gio.bus_own_name_on_connection(
                connection,
                adapter_name,
                Gio.BusNameOwnerFlags.NONE,
                lambda *_: None,
                lambda *_: None,
            )

            def call(method, parameters=None, returns="()"):
                return connection.call_sync(
                    NAME,
                    PATH,
                    NAME,
                    method,
                    parameters,
                    GLib.VariantType.new(returns),
                    Gio.DBusCallFlags.NONE,
                    2000,
                    None,
                )

            def state() -> dict:
                return json.loads(call("GetState", returns="(s)").unpack()[0])

            def name_owned(name: str) -> bool:
                value = connection.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameHasOwner",
                    GLib.Variant("(s)", (name,)),
                    GLib.VariantType.new("(b)"),
                    Gio.DBusCallFlags.NONE,
                    1000,
                    None,
                )
                return bool(value.unpack()[0])

            # 1. Wait for the daemon and validate the state schema.
            wait_until(lambda: state(), 5.0, "daemon to acquire its bus name")
            current = state()
            assert current["version"] == STATE_VERSION, current
            assert "error_code" in current and "error_detail" in current, current
            assert "session_connected" in current, current
            assert current["instance"], current

            # 2. Retry() (no arguments) is exposed for the UI retry button.
            call("Retry")
            assert state()["version"] == STATE_VERSION, state()

            # 3. Manual mode selection (session-bus development backend).
            #    On a non-Panasonic or folded machine the service correctly
            #    refuses with a stable error code instead; both paths are valid
            #    behaviour for this test.
            try:
                call("SetMode", GLib.Variant("(s)", ("stand",)))
            except GLib.Error as error:
                assert any(
                    code in error.message
                    for code in ("invalid-mode", "unsupported-model", "hardware-priority")
                ), error.message
            else:
                wait_until(
                    lambda: state()["manual"] == "stand",
                    2.0,
                    "SetMode stand to apply",
                )

            # 4. Tent prompt preference is persisted to the private config.
            call("SetTentPrompt", GLib.Variant("(b)", (False,)))
            wait_until(lambda: config_path.is_file(), 2.0, "settings file write")
            saved = json.loads(config_path.read_text())
            assert saved.get("version") == 1, saved
            assert saved.get("tent_prompt") is False, saved

            # 5. Invalid mode is rejected with a stable error code.
            try:
                call("SetMode", GLib.Variant("(s)", ("arbitrary-command",)))
            except GLib.Error as error:
                assert any(
                    code in error.message
                    for code in ("invalid-mode", "unsupported-model")
                ), error.message
            else:
                raise AssertionError("invalid mode accepted")

            # 6. SessionUpdate(u b s s) lease handshake from the per-uid
            #    adapter name owner marks the adapter as connected.  Re-send if
            #    the service bumped its generation between GetState and
            #    SessionUpdate.
            wait_until(lambda: name_owned(adapter_name), 2.0, "session name owner")
            connected = False
            deadline = time.monotonic() + 3.0
            while not connected and time.monotonic() < deadline:
                generation = state()["generation"]
                call(
                    "SessionUpdate",
                    GLib.Variant("(ubss)", (generation, True, "", "")),
                )
                connected = state()["session_connected"] is True
                if not connected:
                    time.sleep(0.05)
            assert connected, "session lease did not connect"
            generation = state()["generation"]

            # 7. The old three-argument SessionUpdate signature must be refused.
            try:
                call("SessionUpdate", GLib.Variant("(ubs)", (generation, True, "")))
            except GLib.Error:
                pass
            else:
                raise AssertionError("legacy SessionUpdate(u b s) signature accepted")

            print(
                "D-Bus checks passed: state v2, Retry, mode selection, persisted",
                "preference, error code and SessionUpdate(u b s s) lease",
                f"(per-uid adapter {adapter_name}).",
            )
            return 0
        finally:
            if owner is not None:
                Gio.bus_unown_name(owner)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
