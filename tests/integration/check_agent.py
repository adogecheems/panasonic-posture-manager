#!/usr/bin/python
"""End-to-end session adapter check on an isolated development bus.

Run by ``make check-dbus`` inside ``dbus-run-session``.  A real daemon starts
on the session bus with the normal sensor/switch composition; the real
``Agent`` process shell and ``SessionRuntime`` then own the per-uid adapter
name, apply one display target through an injected fake Display, send a lease
heartbeat and clean up on quit.  No Mutter, notification daemon, system bus or
root-only relay is touched.
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
    session_name,
)
from panasonic_posture.session.agent import Agent  # noqa: E402
from panasonic_posture.session.client import SystemClient  # noqa: E402

DAEMON = Path(__file__).with_name("session_daemon.py")


class RecordingClient:
    def __init__(self, connection) -> None:
        self._client = SystemClient(connection)
        self.updates = []

    def get_state(self):
        return self._client.get_state()

    def session_update(self, *feedback):
        self.updates.append(feedback)
        return self._client.session_update(*feedback)

    def confirm_tent(self, epoch):
        return self._client.confirm_tent(epoch)

    def confirm_stand(self, epoch):
        return self._client.confirm_stand(epoch)


class FakeDisplay:
    def __init__(self) -> None:
        self.applied = []
        self.restores = 0

    def apply(self, target) -> None:
        self.applied.append(target)

    def restore(self, normal: bool = False) -> None:
        self.restores += 1


class FakeNotifier:
    def __init__(self) -> None:
        self.sync_calls = []
        self.close_calls = 0
        self.notification = 0
        self.epoch = None
        self.prompt = None

    def sync(self, epoch, prompt="tent", allow_retry=True, now=None) -> None:
        self.sync_calls.append((epoch, allow_retry))
        self.prompt = prompt

    def close(self) -> None:
        self.close_calls += 1
        self.notification = 0
        self.epoch = None
        self.prompt = None


def wait_for_state(connection, predicate, timeout: float, description: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            reply = connection.call_sync(
                NAME,
                PATH,
                NAME,
                "GetState",
                None,
                GLib.VariantType.new("(s)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
            last = json.loads(reply.unpack()[0])
            if predicate(last):
                return last
        except (GLib.Error, ValueError):
            pass
        time.sleep(0.05)
    raise RuntimeError(f"timeout waiting for {description}: {last}")


def main() -> int:
    if not DAEMON.is_file():
        raise SystemExit(f"missing helper: {DAEMON}")
    with tempfile.TemporaryDirectory(prefix="posture-agent-") as config_dir:
        process = subprocess.Popen(
            [sys.executable, str(DAEMON), str(Path(config_dir) / "settings.json")],
            cwd=str(REPO_ROOT),
        )
        try:
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            wait_for_state(
                connection, lambda state: True, 5.0, "daemon to acquire its name"
            )

            client = RecordingClient(connection)
            display = FakeDisplay()
            notifier = FakeNotifier()
            agent = Agent(
                system_bus=connection,
                session_bus=connection,
                uid=os.getuid(),
                build=lambda current: current.attach(client, display, notifier),
                screen_locked=lambda: False,
                install_signals=False,
            )
            timed_out = []

            def stop_when_ready():
                if agent.ready:
                    agent.loop.quit()
                    return GLib.SOURCE_REMOVE
                if time.monotonic() >= deadline:
                    timed_out.append(True)
                    agent.loop.quit()
                    return GLib.SOURCE_REMOVE
                return GLib.SOURCE_CONTINUE

            deadline = time.monotonic() + 5.0
            GLib.timeout_add(25, stop_when_ready)
            code = agent.serve()
            if timed_out:
                raise RuntimeError("session adapter did not become ready")
            assert code == 0, code
            assert display.applied, "display target was not applied"
            assert display.restores, "display was not restored during cleanup"
            assert notifier.close_calls, "notification was not closed during cleanup"
            assert client.updates and client.updates[-1][1] is True, client.updates

            state = wait_for_state(
                connection,
                lambda value: value.get("session_connected") is True,
                2.0,
                "adapter lease to reach the daemon",
            )
            assert state["generation"] == client.updates[-1][0], state
            print(
                "Agent integration passed: display applied, SessionUpdate lease",
                f"connected for {session_name(os.getuid())}, cleanup restored display.",
            )
            return 0
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
