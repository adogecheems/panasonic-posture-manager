"""Tests for :mod:`panasonic_posture.service.actuator`.

A temporary fake relay executable is spawned for real, which exercises the
heartbeat/report/exit/recover protocol without root, /dev/uinput or a real
relay binary.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from panasonic_posture.protocol import RELAY_HEARTBEAT_SECONDS, ErrorCode
from panasonic_posture.service import actuator as actuator_module
from panasonic_posture.service.actuator import (
    ENV_RELAY,
    RECOVER_TIMEOUT_SECONDS,
    RELEASE_TIMEOUT_SECONDS,
    Actuator,
    ActuatorError,
    RelayState,
)

FAKE_RELAY = r'''#!/usr/bin/python
import json
import os
import select
import signal
import sys

record = os.environ.get("FAKE_RELAY_RECORD")
mode = os.environ.get("FAKE_RELAY_MODE", "normal")
arguments = sys.argv[1:]
if record:
    with open(record, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"argv": arguments, "mode": mode}) + "\n")
if "--recover" in arguments:
    sys.exit(int(os.environ.get("FAKE_RELAY_RECOVER_EXIT", "0")))


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


if mode == "exit":
    sys.exit(3)

emit({"protocol": 2, "kind": "ready", "physical": True})
if mode == "sigkill":
    os.kill(os.getpid(), signal.SIGKILL)
if mode == "garbage":
    sys.stdout.write("this is not json\n")
    sys.stdout.write(json.dumps({"protocol": 1, "kind": "applied"}) + "\n")
    sys.stdout.flush()
applied = {
    "protocol": 2,
    "kind": "applied",
    "hardware": False,
    "tablet": True,
    "input_disabled": True,
    "physical": True,
    "error": None,
}
stdin = sys.stdin.fileno()
while True:
    readable, _, _ = select.select([stdin], [], [], 0.2)
    if not readable:
        continue
    data = os.read(stdin, 64)
    if not data:
        code = int(os.environ.get("FAKE_RELAY_RELEASE_EXIT", "0"))
        emit({"protocol": 2, "kind": "released", "exit": code})
        sys.exit(code)
    if mode == "silent":
        continue
    emit(applied)
'''

PROFILE = SimpleNamespace(
    switch_name="Intel HID switches",
    keyboard_name="AT Translated Set 2 keyboard",
    touchpad_name="ELAN900C:00 04F3:2D5F Touchpad",
)


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class FixedClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeRelayTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.record = self.root / "relay.log"
        self.script = self.root / "relay"
        self.script.write_text(FAKE_RELAY, encoding="utf-8")
        self.script.chmod(stat.S_IRWXU)
        environment = mock.patch.dict(
            os.environ,
            {"FAKE_RELAY_RECORD": str(self.record), "FAKE_RELAY_MODE": "normal"},
        )
        environment.start()
        self.addCleanup(environment.stop)
        self.clock = FixedClock()
        self.actuators: list[Actuator] = []

    def tearDown(self) -> None:
        for actuator in self.actuators:
            actuator.release()

    def make(self, *, profile: object = PROFILE) -> Actuator:
        actuator = Actuator(
            profile,
            executable=self.script,
            monotonic=self.clock,
            logger=mock.Mock(),
        )
        self.actuators.append(actuator)
        return actuator

    def record_entries(self) -> list[dict]:
        if not self.record.exists():
            return []
        return [
            json.loads(line)
            for line in self.record.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def start_and_wait(self, actuator: Actuator) -> None:
        actuator.keep(self.clock.value)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            self.clock.value += 0.05
            actuator.keep(self.clock.value)
            if actuator.state is not None:
                return
            time.sleep(0.01)
        self.fail(
            "relay state was never reported "
            f"(error_code={actuator.error_code}, detail={actuator.error_detail})"
        )


class CommandTests(unittest.TestCase):
    def test_profile_arguments_and_recover(self) -> None:
        actuator = Actuator(PROFILE, executable="/opt/relay")
        self.assertEqual(
            actuator.command(),
            [
                "/opt/relay",
                "--switch-name",
                "Intel HID switches",
                "--keyboard-name",
                "AT Translated Set 2 keyboard",
                "--touchpad-name",
                "ELAN900C:00 04F3:2D5F Touchpad",
            ],
        )
        self.assertEqual(actuator.command(recover=True)[-1], "--recover")

    def test_generic_profile_has_no_name_arguments(self) -> None:
        actuator = Actuator(
            SimpleNamespace(switch_name=None, keyboard_name=None, touchpad_name=None),
            executable="/opt/relay",
        )
        self.assertEqual(actuator.command(), ["/opt/relay"])

    def test_executable_environment_override(self) -> None:
        actuator = Actuator(executable=None, environ={ENV_RELAY: "/custom/relay"})
        self.assertEqual(actuator.executable, Path("/custom/relay"))

    def test_executable_development_fallback(self) -> None:
        # The host may have the package installed (so the installed relay path
        # exists); the repository fallback must still be selected when it does
        # not.
        with mock.patch(
            "panasonic_posture.service.actuator.RELAY_INSTALL_PATH",
            "/nonexistent/panasonic-posture-manager/relay",
        ):
            actuator = Actuator(environ={})
        self.assertTrue(actuator.executable.is_absolute())
        self.assertEqual(actuator.executable.parts[-2:], ("backend", "relay"))
        expected = (
            Path(actuator_module.__file__).resolve().parents[3]
            / "backend"
            / "relay"
        )
        self.assertEqual(actuator.executable, expected)

    def test_explicit_executable_wins(self) -> None:
        actuator = Actuator(executable="/opt/relay", environ={ENV_RELAY: "/x"})
        self.assertEqual(actuator.executable, Path("/opt/relay"))


class RelayStateTests(unittest.TestCase):
    def test_applied_message(self) -> None:
        state = RelayState.from_message(
            {
                "protocol": 2,
                "kind": "applied",
                "hardware": False,
                "tablet": True,
                "input_disabled": True,
                "physical": True,
                "error": None,
            }
        )
        self.assertEqual(
            state,
            RelayState(
                tablet=True,
                input_disabled=True,
                hardware=False,
                physical=True,
                error=None,
            ),
        )

    def test_applied_message_error(self) -> None:
        state = RelayState.from_message(
            {"tablet": False, "input_disabled": True, "error": "input-grab-failed"}
        )
        self.assertEqual(state.error, "input-grab-failed")
        self.assertFalse(state.tablet)

    def test_non_string_error_is_dropped(self) -> None:
        self.assertIsNone(RelayState.from_message({"error": 5}).error)


class ActuatorLifecycleTests(FakeRelayTestCase):
    def test_spawn_reports_applied_state_and_heartbeats(self) -> None:
        actuator = self.make()
        self.start_and_wait(actuator)
        self.assertIsNone(actuator.error_code)
        self.assertTrue(actuator.running)
        self.assertEqual(
            actuator.state,
            RelayState(True, True, False, True, None),
        )
        self.assertIs(actuator.physical, True)
        self.assertGreaterEqual(actuator.last_report, self.clock.value)

    def test_heartbeat_is_rate_limited(self) -> None:
        actuator = self.make()
        self.start_and_wait(actuator)
        # start_and_wait advances the fake clock by an amount that depends on
        # process startup latency.  Establish a fresh heartbeat baseline before
        # asserting the rate limit, otherwise a slow first startup makes the
        # clock advance look like a legitimate heartbeat interval.
        previous = actuator.last_sent
        self.assertIsNotNone(previous)
        self.clock.value = max(
            self.clock.value, previous + RELAY_HEARTBEAT_SECONDS + 0.01
        )
        actuator.keep(self.clock.value)
        sent = actuator.last_sent
        self.assertIsNotNone(sent)
        self.assertEqual(sent, self.clock.value)
        self.clock.value += 0.1
        actuator.keep(self.clock.value)
        self.assertEqual(actuator.last_sent, sent)
        self.clock.value += RELAY_HEARTBEAT_SECONDS
        actuator.keep(self.clock.value)
        self.assertGreater(actuator.last_sent, sent)

    def test_report_timeout_raises_and_sets_error(self) -> None:
        os.environ["FAKE_RELAY_MODE"] = "silent"
        actuator = self.make()
        actuator.keep(self.clock.value)
        self.clock.value += 1.0
        actuator.keep(self.clock.value)
        self.clock.value += 3.0
        with self.assertRaises(ActuatorError) as caught:
            actuator.keep(self.clock.value)
        self.assertEqual(caught.exception.code, ErrorCode.RELAY_HEARTBEAT)
        self.assertEqual(actuator.error_code, ErrorCode.RELAY_HEARTBEAT)
        with self.assertRaises(ActuatorError):
            actuator.keep(self.clock.value)

    def test_unexpected_exit_sets_relay_exited_error(self) -> None:
        os.environ["FAKE_RELAY_MODE"] = "exit"
        actuator = self.make()
        actuator.keep(self.clock.value)
        self.assertTrue(
            wait_for(lambda: actuator.process is not None and actuator.process.poll() is not None),
            "fake relay did not exit",
        )
        self.clock.value += 0.1
        with self.assertRaises(ActuatorError) as caught:
            actuator.keep(self.clock.value)
        self.assertEqual(caught.exception.code, ErrorCode.RELAY_EXITED)
        self.assertIn("status 3", actuator.error_detail or "")

    def test_start_failure_sets_stable_error(self) -> None:
        actuator = Actuator(
            PROFILE,
            executable=self.root / "does-not-exist",
            monotonic=self.clock,
            logger=mock.Mock(),
        )
        self.actuators.append(actuator)
        with self.assertRaises(ActuatorError) as caught:
            actuator.keep(self.clock.value)
        self.assertEqual(caught.exception.code, ErrorCode.RELAY_START)
        self.assertEqual(actuator.error_code, ErrorCode.RELAY_START)
        with self.assertRaises(ActuatorError):
            actuator.keep(self.clock.value)

    def test_malformed_and_wrong_protocol_reports_are_ignored(self) -> None:
        os.environ["FAKE_RELAY_MODE"] = "garbage"
        actuator = self.make()
        self.start_and_wait(actuator)
        self.assertIsNone(actuator.error_code)
        self.assertEqual(actuator.state, RelayState(True, True, False, True, None))

    def test_retry_clears_error_and_allows_restart(self) -> None:
        os.environ["FAKE_RELAY_MODE"] = "normal"
        actuator = self.make()
        actuator.keep(self.clock.value)
        self.start_and_wait(actuator)
        actuator.error_code = ErrorCode.RELAY_HEARTBEAT
        actuator.error_detail = "synthetic"
        actuator.retry()
        self.assertIsNone(actuator.error_code)
        self.assertIsNone(actuator.process)
        self.start_and_wait(actuator)

    def test_retry_preserves_failed_native_recovery(self) -> None:
        os.environ["FAKE_RELAY_RECOVER_EXIT"] = "5"
        self.addCleanup(os.environ.pop, "FAKE_RELAY_RECOVER_EXIT", None)
        actuator = self.make()
        actuator.error_code = ErrorCode.RELAY_HEARTBEAT
        actuator.error_detail = "synthetic"
        actuator._needs_recovery = True

        actuator.retry()

        self.assertEqual(actuator.error_code, ErrorCode.NATIVE_RECOVERY)
        self.assertIn("status 5", actuator.error_detail or "")
        self.assertTrue(actuator._needs_recovery)


class ActuatorReleaseTests(FakeRelayTestCase):
    def test_clean_release_relies_on_relay_cleanup(self) -> None:
        # The relay repairs the native switch itself on EOF; a second
        # ``--recover`` would re-create the virtual device and emit another
        # ON/final sequence (touch-mode flicker on every mode change).
        actuator = self.make()
        self.start_and_wait(actuator)
        actuator.release()
        self.assertIsNone(actuator.process)
        self.assertIsNone(actuator.state)
        self.assertFalse(actuator._needs_recovery)
        self.assertIsNone(actuator.error_code)
        entries = self.record_entries()
        self.assertEqual(len(entries), 1)
        self.assertNotIn("--recover", entries[0]["argv"])
        self.assertEqual(entries[0]["argv"][0], "--switch-name")

    def test_failed_relay_cleanup_runs_recovery(self) -> None:
        os.environ["FAKE_RELAY_RELEASE_EXIT"] = "8"
        self.addCleanup(os.environ.pop, "FAKE_RELAY_RELEASE_EXIT", None)
        actuator = self.make()
        self.start_and_wait(actuator)

        actuator.release()

        self.assertFalse(actuator._needs_recovery)
        self.assertIsNone(actuator.error_code)
        entries = self.record_entries()
        self.assertEqual(len(entries), 2)
        self.assertIn("--recover", entries[1]["argv"])

    def test_signal_death_recovery_runs_on_next_release(self) -> None:
        os.environ["FAKE_RELAY_MODE"] = "sigkill"
        actuator = self.make()
        actuator.keep(self.clock.value)
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and actuator.error_code is None:
            self.clock.value += 0.05
            try:
                actuator.keep(self.clock.value)
            except ActuatorError:
                pass
            time.sleep(0.01)
        self.assertEqual(actuator.error_code, ErrorCode.RELAY_EXITED)
        self.assertTrue(actuator._needs_recovery)
        actuator.release()
        self.assertFalse(actuator._needs_recovery)
        entries = self.record_entries()
        self.assertTrue(any("--recover" in entry["argv"] for entry in entries))

    def test_release_without_running_relay_does_not_recover(self) -> None:
        actuator = self.make()
        actuator.release()
        time.sleep(0.1)
        self.assertEqual(self.record_entries(), [])

    def test_release_bounds_unresponsive_relay(self) -> None:
        class StubbornProcess:
            def __init__(self) -> None:
                self.stdin = mock.Mock()
                self.stdout = mock.Mock()
                self.stderr = None
                self.killed = False
                self.wait_timeouts = []

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.wait_timeouts.append(timeout)
                if not self.killed:
                    raise subprocess.TimeoutExpired("relay", timeout)
                return 0

            def kill(self):
                self.killed = True

        process = StubbornProcess()
        runner = mock.Mock(
            return_value=SimpleNamespace(returncode=0)
        )
        actuator = Actuator(
            PROFILE,
            executable=self.script,
            monotonic=self.clock,
            logger=mock.Mock(),
            popen=lambda *args, **kwargs: process,
            runner=runner,
        )
        self.actuators.append(actuator)
        actuator.keep(self.clock.value)
        started = time.monotonic()
        actuator.release()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 1.2)
        self.assertTrue(process.killed)
        self.assertIsNone(actuator.process)
        # Both mandatory waits are bounded well below the 3 s software lease.
        self.assertEqual(
            process.wait_timeouts, [RELEASE_TIMEOUT_SECONDS, RELEASE_TIMEOUT_SECONDS]
        )
        self.assertEqual(RELEASE_TIMEOUT_SECONDS, 1.5)
        self.assertEqual(RECOVER_TIMEOUT_SECONDS, 1.5)
        recover_argv = runner.call_args.args[0]
        self.assertIn("--recover", recover_argv)
        self.assertEqual(runner.call_args.kwargs["timeout"], RECOVER_TIMEOUT_SECONDS)

    def test_recover_failure_sets_native_recovery_error(self) -> None:
        os.environ["FAKE_RELAY_RECOVER_EXIT"] = "5"
        self.addCleanup(os.environ.pop, "FAKE_RELAY_RECOVER_EXIT", None)
        actuator = self.make()
        actuator._needs_recovery = True
        self.assertFalse(actuator.recover())
        self.assertEqual(actuator.error_code, ErrorCode.NATIVE_RECOVERY)
        self.assertIn("status 5", actuator.error_detail or "")
        self.assertTrue(actuator._needs_recovery)


if __name__ == "__main__":
    unittest.main()
