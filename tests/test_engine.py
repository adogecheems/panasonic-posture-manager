"""Tests for :mod:`panasonic_posture.service.engine` (all dependencies faked)."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from panasonic_posture.model import (
    HardwareStatus,
    Mode,
    Pose,
    SensorReading,
    SensorSource,
    Target,
)
from panasonic_posture.protocol import ErrorCode
from panasonic_posture.service.access import AccessDenied
from panasonic_posture.service.actuator import ActuatorError, RelayState
from panasonic_posture.service.engine import Engine, ServiceError, guard
from panasonic_posture.service.settings import Settings

LAPTOP = Target(mode="laptop", tablet=False, rotation=0, disable_internal_input=False)
TENT = Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True)
STAND = Target(mode="stand", tablet=True, rotation=0, disable_internal_input=True)
HARDWARE = Target(mode="hardware", tablet=True, rotation=None, disable_internal_input=False)


class FakeClock:
    def __init__(self, value: float = 100.0, step: float = 0.0) -> None:
        self.value = value
        self.step = step

    def __call__(self) -> float:
        value = self.value
        self.value += self.step
        return value


class FakeController:
    """Mirror of the real Controller interface owned by the core-domain task."""

    def __init__(self) -> None:
        self.policy = SimpleNamespace(mode="D", stable=False)
        self.manual = Mode.AUTO
        self.tent_prompt = True
        self.target = Target()
        self.generation = 7
        self.applied: Target | None = None
        self.error_code: str | None = None
        self.error_detail: str | None = None
        self.hardware: bool | None = None
        self.pending_epoch: int | None = None
        self.update_error: Exception | None = None
        self.fail_ack = False
        self.update_calls: list = []
        self.acknowledge_calls: list = []
        self.set_manual_calls: list = []
        self.confirm_calls: list = []
        self.confirm_result = True
        self.reset_calls = 0

    @property
    def pending_notification(self) -> int | None:
        return self.pending_epoch

    def update(self, now, reading, hardware):
        self.update_calls.append((now, reading, hardware))
        if self.update_error is not None:
            raise self.update_error
        self.hardware = hardware
        return self.generation, self.target

    def set_manual(self, mode) -> None:
        value = str(mode)
        if value not in {item.value for item in Mode}:
            raise ValueError(ErrorCode.INVALID_MODE)
        if self.hardware is True and value != "auto":
            raise ValueError(ErrorCode.HARDWARE_PRIORITY)
        self.manual = Mode(value)
        self.set_manual_calls.append(value)

    def acknowledge(self, generation, success, error_code=None, error_detail=None):
        self.acknowledge_calls.append((generation, success, error_code, error_detail))
        if self.fail_ack or generation != self.generation:
            return False
        if success:
            self.applied = self.target
            self.error_code = None
            self.error_detail = None
        else:
            self.applied = None
            self.error_code = error_code or ErrorCode.INTERNAL
            self.error_detail = error_detail
        return True

    def confirm_tent(self, epoch) -> bool:
        self.confirm_calls.append(epoch)
        return self.confirm_result

    def reset_after_suspend(self) -> None:
        self.reset_calls += 1
        self.manual = Mode.AUTO
        self.hardware = None
        self.generation += 1
        self.applied = None
        self.error_code = None
        self.error_detail = None
        self.target = Target()


class FakeSensor:
    def __init__(self) -> None:
        self._reading = SensorReading()
        self.source = SensorSource.STARTING
        self.sensor_error: str | None = None
        self.reading_error: Exception | None = None
        self.prefer_shared_calls: list[bool] = []
        self.started = 0
        self.closed = 0

    def start(self) -> None:
        self.started += 1

    def close(self) -> None:
        self.closed += 1

    def reading(self) -> SensorReading:
        if self.reading_error is not None:
            raise self.reading_error
        return self._reading

    def set_prefer_shared(self, value: bool) -> None:
        self.prefer_shared_calls.append(value)


class FakeActuator:
    def __init__(self) -> None:
        self.running = False
        self.state: RelayState | None = None
        self.error_code: str | None = None
        self.error_detail: str | None = None
        self.calls: list[str] = []
        self.keep_state: RelayState | None = None
        self.keep_error: ActuatorError | None = None
        self.keep_hook = None
        self.release_hook = None

    def keep(self, now) -> None:
        self.calls.append("keep")
        if self.keep_hook is not None:
            self.keep_hook()
        if self.keep_error is not None:
            raise self.keep_error
        self.running = True
        if self.keep_state is not None:
            self.state = self.keep_state

    def release(self) -> None:
        self.calls.append("release")
        if self.release_hook is not None:
            self.release_hook()
        self.running = False
        self.state = None

    def retry(self) -> None:
        self.calls.append("retry")
        self.error_code = None
        self.error_detail = None
        self.running = False
        self.state = None


class FakeAccess:
    def __init__(self) -> None:
        self.deny = False
        self.checks: list[int] = []

    def active_session(self, uid):
        self.checks.append(uid)
        if self.deny:
            raise AccessDenied("no active session")
        return SimpleNamespace(uid=uid)


class FakePresentation:
    def __init__(self) -> None:
        self.calls: list = []
        self.value = Pose.UNKNOWN

    def update(self, now, candidate, hardware, stamp):
        self.calls.append((now, candidate, hardware, stamp))
        if hardware is True:
            self.value = Pose.TABLET
        elif str(candidate) != Pose.UNKNOWN:
            self.value = Pose(str(candidate))
        return self.value


class FakeSettings:
    def __init__(self, tent_prompt: bool = True) -> None:
        self.value = Settings(tent_prompt=tent_prompt)
        self.saved: list[Settings] = []
        self.fail = False
        self.load_error: Exception | None = None

    def load(self) -> Settings:
        if self.load_error is not None:
            raise self.load_error
        return self.value

    def save(self, settings: Settings) -> None:
        if self.fail:
            raise OSError("disk full")
        self.saved.append(settings)
        self.value = settings


def panasonic_profile() -> SimpleNamespace:
    return SimpleNamespace(
        panasonic=True,
        id="cfqv8",
        product="CFQV8-1L",
        vendor="Panasonic Corporation",
        accel_name="accel_3d",
        mount_matrix=None,
        switch_name=None,
    )


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mono = FakeClock(100.0)
        self.boot = FakeClock(1000.0)
        self.profile = panasonic_profile()
        self.controller = FakeController()
        self.sensor = FakeSensor()
        self.actuator = FakeActuator()
        self.access = FakeAccess()
        self.presentation = FakePresentation()
        self.settings = FakeSettings()
        self.listener = mock.Mock()
        self.hardware = False
        self.hardware_status = HardwareStatus.OK
        self.hardware_error: Exception | None = None
        self.hardware_queries = 0

    def query(self):
        self.hardware_queries += 1
        if self.hardware_error is not None:
            raise self.hardware_error
        return self.hardware, self.hardware_status

    def make(self, **overrides) -> Engine:
        kwargs = dict(
            controller=self.controller,
            sensor=self.sensor,
            actuator=self.actuator,
            access=self.access,
            presentation=self.presentation,
            settings=self.settings,
            profile=self.profile,
            hardware_query=self.query,
            instance="0123456789abcdef",
            development=False,
            stale_seconds=2.0,
            monotonic=self.mono,
            clock=self.boot,
            on_state=self.listener,
            logger=mock.Mock(),
        )
        kwargs.update(overrides)
        return Engine(**kwargs)

    def tick(self, engine: Engine, now: float | None = None) -> None:
        if now is not None:
            self.mono.value = now
        engine.tick(self.mono.value)

    def lease(self, engine: Engine, *, generation: int | None = None, ready: bool = True,
              code: str | None = None, detail: str | None = None, uid: int = 1000) -> bool:
        return engine.session_update(
            self.controller.generation if generation is None else generation,
            ready,
            code,
            detail,
            uid,
        )


class StateSnapshotTests(EngineTestCase):
    def test_initial_state_uses_schema_version_two(self) -> None:
        engine = self.make()
        state = engine.state.to_dict()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["instance"], "0123456789abcdef")
        self.assertEqual(state["supported"], True)
        self.assertEqual(state["backend"], "ready")
        self.assertEqual(state["profile"], "cfqv8")
        self.assertEqual(state["model"], "CFQV8-1L")
        self.assertEqual(state["vendor"], "Panasonic Corporation")
        self.assertEqual(state["sensor_source"], "starting")
        parsed = json.loads(engine.state_json())
        self.assertEqual(parsed["version"], 2)

    def test_tick_builds_documented_state(self) -> None:
        self.sensor._reading = SensorReading((0.0, 0.0, -9.81), 1000.0, SensorSource.IIO)
        self.sensor.source = SensorSource.IIO
        self.sensor.sensor_error = None
        engine = self.make()
        self.tick(engine)
        state = engine.state.to_dict()
        self.assertEqual(state["hardware"], False)
        self.assertEqual(state["hardware_status"], "ok")
        self.assertEqual(state["target"]["mode"], "unavailable")
        self.assertEqual(state["sensor_source"], "iio")
        self.assertIsNone(state["error_code"])

    def test_state_listener_fires_only_on_change(self) -> None:
        engine = self.make()
        self.tick(engine)
        calls = self.listener.call_count
        self.assertGreaterEqual(calls, 1)
        self.tick(engine)
        self.assertEqual(self.listener.call_count, calls)
        self.hardware = True
        self.tick(engine)
        self.assertEqual(self.listener.call_count, calls + 1)
        self.assertEqual(self.listener.call_args.args[0].hardware, True)

    def test_listener_exception_does_not_break_tick(self) -> None:
        self.listener.side_effect = RuntimeError("listener failed")
        engine = self.make()
        self.tick(engine)  # must not raise
        self.tick(engine)

    def test_development_backend_disables_actuator(self) -> None:
        engine = self.make(development=True)
        self.tick(engine)
        self.assertEqual(engine.state.backend.value, "development")
        self.assertEqual(self.actuator.calls, [])
        self.lease(engine)
        self.controller.target = TENT
        self.tick(engine)
        self.assertEqual(self.actuator.calls, [])

    def test_unsupported_model_is_gated(self) -> None:
        profile = panasonic_profile()
        profile.panasonic = False
        self.profile = profile
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.tick(engine)
        state = engine.state
        self.assertFalse(state.supported)
        self.assertEqual(state.backend.value, "unsupported")
        self.assertEqual(state.target, Target(mode="unavailable"))
        self.assertIsNone(state.pending_notification)
        self.assertEqual(self.actuator.calls, [])
        with self.assertRaises(ServiceError) as caught:
            engine.set_mode("tent")
        self.assertEqual(caught.exception.code, ErrorCode.UNSUPPORTED_MODEL)
        with self.assertRaises(ServiceError) as caught:
            engine.confirm_tent(1)
        self.assertEqual(caught.exception.code, ErrorCode.UNSUPPORTED_MODEL)

    def test_settings_apply_tent_prompt_at_construction(self) -> None:
        self.settings = FakeSettings(tent_prompt=False)
        engine = self.make()
        self.assertFalse(engine.state.tent_prompt)
        self.assertFalse(self.controller.tent_prompt)

    def test_settings_load_failure_keeps_defaults(self) -> None:
        self.settings.load_error = OSError("cannot read")
        engine = self.make()
        self.assertTrue(engine.state.tent_prompt)


class HardwareDebounceTests(EngineTestCase):
    def test_transient_unknown_keeps_previous_value_then_releases(self) -> None:
        engine = self.make()
        self.hardware = True
        self.tick(engine, now=100.0)
        self.assertIs(engine.state.hardware, True)
        self.hardware = None
        self.hardware_status = HardwareStatus.READ_ERROR
        self.tick(engine, now=100.1)
        self.assertIs(engine.state.hardware, True)  # single frame is debounced
        self.tick(engine, now=100.4)
        self.assertIs(engine.state.hardware, True)  # 0.3 s < 0.5 s
        self.tick(engine, now=100.7)
        self.assertIsNone(engine.state.hardware)  # 0.6 s >= 0.5 s
        self.assertEqual(engine.state.hardware_status.value, "read_error")
        self.hardware = True
        self.hardware_status = HardwareStatus.OK
        self.tick(engine, now=100.8)
        self.assertIs(engine.state.hardware, True)

    def test_hardware_query_exception_degrades_to_unknown(self) -> None:
        engine = self.make()
        self.hardware_error = OSError("sysfs broken")
        self.tick(engine)
        self.assertIsNone(engine.state.hardware)
        self.assertEqual(engine.state.hardware_status.value, "read_error")
        self.assertIsNone(engine.state.error_code)
        self.hardware_error = None
        self.hardware = False
        self.tick(engine)
        self.assertIs(engine.state.hardware, False)

    def test_non_boolean_hardware_value_is_unknown(self) -> None:
        engine = self.make()
        self.hardware = 1  # type: ignore[assignment]
        self.tick(engine)
        self.assertIsNone(engine.state.hardware)

    def test_prefer_shared_tracks_hardware_changes(self) -> None:
        engine = self.make()
        self.tick(engine)
        self.tick(engine)
        self.assertEqual(self.sensor.prefer_shared_calls, [False])
        self.hardware = True
        self.tick(engine)
        self.assertEqual(self.sensor.prefer_shared_calls, [False, True])
        self.tick(engine)
        self.assertEqual(self.sensor.prefer_shared_calls, [False, True])

    def test_prefer_shared_uses_sysfs_during_software_takeover(self) -> None:
        engine = self.make()
        self.controller.policy.mode = "P"
        self.controller.target = LAPTOP
        self.tick(engine)
        self.assertEqual(self.sensor.prefer_shared_calls, [True])

        self.controller.policy.mode = "L"
        self.controller.target = LAPTOP
        self.tick(engine)
        self.assertEqual(self.sensor.prefer_shared_calls, [True, False])

        self.controller.policy.mode = "T"
        self.controller.target = TENT
        self.tick(engine)
        self.assertEqual(self.sensor.prefer_shared_calls, [True, False, True])

        self.controller.policy.mode = "L"
        self.controller.target = TENT
        self.tick(engine)
        self.assertEqual(
            self.sensor.prefer_shared_calls, [True, False, True]
        )


class ActuatorTransactionTests(EngineTestCase):
    def test_successful_applied_report_acknowledges_target(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.tick(engine)
        self.assertIn("keep", self.actuator.calls)
        self.assertEqual(
            self.controller.acknowledge_calls[-1][0:3],
            (self.controller.generation, True, None),
        )
        self.assertEqual(engine.state.applied, TENT)

    def test_waiting_for_input_release_is_not_an_error(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        # The relay is still waiting for pressed keys/contact to clear.  This
        # is a transient wait, not a failed grab, so the UI must stay calm.
        self.actuator.keep_state = RelayState(True, False, False, True, None)
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["keep"])
        self.assertEqual(self.controller.acknowledge_calls, [])
        self.assertIsNone(engine.state.error_code)
        self.assertIsNone(engine.state.applied)

    def test_no_ack_without_session_lease(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.tick(engine)
        self.assertNotIn("keep", self.actuator.calls)
        self.assertIn("release", self.actuator.calls)
        self.assertIsNone(engine.state.applied)

    def test_display_error_from_agent_is_reported_and_releases(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine, ready=False, code=ErrorCode.DISPLAY_TRANSFORM, detail="x")
        self.tick(engine)
        self.assertIn("release", self.actuator.calls)
        self.assertIsNone(engine.state.applied)
        self.assertEqual(engine.state.error_code, ErrorCode.DISPLAY_TRANSFORM)
        self.assertEqual(engine.state.backend.value, "error")
        self.assertTrue(engine.state.session_connected)

    def test_actuator_failure_acknowledges_error(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.actuator.keep_error = ActuatorError(ErrorCode.RELAY_HEARTBEAT, "no reports")
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.RELAY_HEARTBEAT)
        self.assertIsNone(engine.state.applied)

    def test_slow_actuator_release_does_not_extend_the_lease(self) -> None:
        engine = self.make()
        self.controller.target = LAPTOP
        self.lease(engine)
        delayed = self.mono.value + 4.0

        def slow_release() -> None:
            self.mono.value = delayed

        self.actuator.release_hook = slow_release
        self.tick(engine, now=100.0)
        self.assertIn("release", self.actuator.calls)
        # The lease expired while the blocking release ran; the published state
        # must reflect the fresh clock, not the pre-blocking one.
        self.assertFalse(engine.state.session_connected)
        self.assertIsNone(engine.state.applied)

    def test_state_json_is_a_cached_snapshot(self) -> None:
        engine = self.make()
        self.assertEqual(self.hardware_queries, 0)
        payload = engine.state_json()
        self.assertEqual(json.loads(payload)["version"], 2)
        # GetState must not trigger a transaction/hardware enumeration (Q-06).
        self.assertEqual(self.hardware_queries, 0)
        self.assertEqual(engine.state.target.mode, "unavailable")

    def test_laptop_target_is_acknowledged_after_release(self) -> None:
        engine = self.make()
        self.controller.target = LAPTOP
        self.lease(engine)
        self.tick(engine)
        self.assertIn("release", self.actuator.calls)
        self.assertEqual(self.controller.acknowledge_calls[-1][1], True)
        self.assertEqual(engine.state.applied, LAPTOP)

    def test_switch_between_software_orientations_keeps_relay(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        # Lease is present but the display prepared an older generation.
        engine.agent_uid = 1000
        engine.agent_seen = self.mono.value
        engine.agent_ready = self.controller.generation - 1
        self.actuator.running = True
        self.actuator.state = RelayState(True, True, False, True, None)
        self.tick(engine)
        self.assertEqual(self.actuator.calls.count("keep"), 1)
        self.assertNotIn("release", self.actuator.calls)

    def test_lease_expiry_releases_and_clears_applied(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.tick(engine, now=100.0)
        self.assertTrue(engine.state.session_connected)
        self.tick(engine, now=103.5)
        self.assertFalse(engine.state.session_connected)
        self.assertIsNone(engine.state.applied)

    def test_not_ready_heartbeat_without_error_is_not_a_backend_error(self) -> None:
        engine = self.make()
        self.controller.applied = LAPTOP
        self.assertTrue(self.lease(engine, ready=False))
        self.assertIsNone(engine.agent_error_code)
        self.assertIsNone(engine.agent_ready)
        self.assertIsNone(self.controller.applied)
        self.tick(engine)
        self.assertIsNone(engine.state.error_code)

    def test_display_error_echo_is_exposed_for_the_ui(self) -> None:
        engine = self.make()
        self.lease(engine, ready=False, code=ErrorCode.DISPLAY_UNAVAILABLE, detail="no-mutter")
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.DISPLAY_UNAVAILABLE)
        self.assertEqual(engine.state.error_detail, "no-mutter")

    def test_unknown_adapter_error_code_maps_to_internal(self) -> None:
        engine = self.make()
        self.lease(engine, ready=False, code="agent-specific-code", detail="x")
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.INTERNAL)
        self.assertIn("agent-specific-code", engine.state.error_detail or "")

    def test_stale_session_update_is_ignored(self) -> None:
        engine = self.make()
        accepted = self.lease(engine, generation=self.controller.generation - 1)
        self.assertFalse(accepted)
        self.assertIsNone(engine.agent_ready)

    def test_tick_performs_no_blocking_logind_check(self) -> None:
        # The daemon authenticates every SessionUpdate (Q-01); the engine tick
        # must stay free of blocking logind/polkit calls (Q-03).
        engine = self.make()
        self.lease(engine)
        self.tick(engine, now=100.0)
        self.tick(engine, now=100.5)
        self.assertEqual(self.access.checks, [])

    def test_heartbeat_rejected_by_daemon_does_not_refresh_lease(self) -> None:
        # A rejected heartbeat never reaches engine.session_update(), so the
        # lease simply expires after LEASE_SECONDS.
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.tick(engine, now=100.0)
        self.assertTrue(engine.state.session_connected)
        self.tick(engine, now=103.5)
        self.assertFalse(engine.state.session_connected)
        self.assertIsNone(engine.state.applied)

    def test_other_uid_cannot_steal_an_active_lease(self) -> None:
        engine = self.make()
        self.lease(engine, uid=1000)
        with self.assertRaises(ServiceError) as caught:
            self.lease(engine, uid=2000)
        self.assertEqual(caught.exception.code, ErrorCode.ACCESS_DENIED)
        self.assertEqual(caught.exception.detail, "session-lease-busy")
        self.mono.value += 4.0
        self.assertTrue(self.lease(engine, uid=2000))


class ModeMethodTests(EngineTestCase):
    def test_set_mode_is_idempotent(self) -> None:
        engine = self.make()
        generation = self.controller.generation
        self.assertFalse(engine.set_mode("auto"))
        self.assertEqual(self.controller.generation, generation)
        self.assertEqual(self.controller.set_manual_calls, [])
        self.assertNotIn("retry", self.actuator.calls)

    def test_set_mode_bumps_generation_and_clears_ack(self) -> None:
        engine = self.make()
        engine.agent_ready = 7
        self.controller.applied = LAPTOP
        generation = self.controller.generation
        self.assertTrue(engine.set_mode("tent"))
        self.assertEqual(self.controller.generation, generation + 1)
        self.assertEqual(self.controller.set_manual_calls, ["tent"])
        self.assertIsNone(self.controller.applied)
        self.assertIsNone(engine.agent_ready)
        self.assertIn("retry", self.actuator.calls)

    def test_set_mode_rejects_unknown_mode(self) -> None:
        engine = self.make()
        with self.assertRaises(ServiceError) as caught:
            engine.set_mode("sideways")
        self.assertEqual(caught.exception.code, ErrorCode.INVALID_MODE)

    def test_set_mode_respects_hardware_priority(self) -> None:
        engine = self.make()
        self.hardware = True
        self.tick(engine)
        with self.assertRaises(ServiceError) as caught:
            engine.set_mode("tent")
        self.assertEqual(caught.exception.code, ErrorCode.HARDWARE_PRIORITY)

    def test_set_mode_does_not_touch_actuator_in_development(self) -> None:
        engine = self.make(development=True)
        self.assertTrue(engine.set_mode("tent"))
        self.assertNotIn("retry", self.actuator.calls)

    def test_set_tent_prompt_persists_and_is_idempotent(self) -> None:
        engine = self.make()
        self.assertTrue(engine.set_tent_prompt(False))
        self.assertEqual(self.settings.saved, [Settings(tent_prompt=False)])
        self.assertFalse(self.controller.tent_prompt)
        self.assertFalse(engine.set_tent_prompt(False))
        self.assertEqual(len(self.settings.saved), 1)

    def test_set_tent_prompt_write_failure_keeps_old_value(self) -> None:
        engine = self.make()
        self.settings.fail = True
        with self.assertRaises(ServiceError) as caught:
            engine.set_tent_prompt(False)
        self.assertEqual(caught.exception.code, ErrorCode.CONFIG_WRITE)
        self.assertTrue(self.controller.tent_prompt)
        self.assertTrue(engine.state.tent_prompt)

    def test_confirm_tent_success_and_expiry(self) -> None:
        engine = self.make()
        self.controller.confirm_result = True
        self.assertTrue(engine.confirm_tent(3))
        self.assertEqual(self.controller.confirm_calls[-1], 3)
        self.controller.confirm_result = False
        with self.assertRaises(ServiceError) as caught:
            engine.confirm_tent(4)
        self.assertEqual(caught.exception.code, ErrorCode.EXPIRED_CONFIRMATION)


class RetryTests(EngineTestCase):
    def test_retry_clears_backend_errors_and_bumps_generation(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.tick(engine)
        self.assertEqual(engine.state.applied, TENT)
        # Simulate a latched relay failure on the same target/generation (the
        # exact state in which SetMode is idempotent and cannot recover, Q-02).
        self.actuator.error_code = ErrorCode.RELAY_EXITED
        self.actuator.error_detail = "relay exited with status 3"
        self.controller.error_code = ErrorCode.RELAY_EXITED
        self.controller.error_detail = "relay exited with status 3"
        engine.agent_ready = self.controller.generation
        generation = self.controller.generation
        self.assertTrue(engine.retry())
        self.assertEqual(self.controller.generation, generation + 1)
        self.assertIn("retry", self.actuator.calls)
        self.assertIsNone(self.actuator.error_code)
        self.assertIsNone(self.controller.error_code)
        self.assertIsNone(self.controller.applied)
        self.assertIsNone(engine.agent_ready)
        self.tick(engine)
        self.assertIsNone(engine.state.error_code)

    def test_retry_replays_the_target_after_display_is_ready(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.tick(engine)
        self.actuator.error_code = ErrorCode.RELAY_HEARTBEAT
        self.controller.error_code = ErrorCode.RELAY_HEARTBEAT
        self.assertTrue(engine.retry())
        new_generation = self.controller.generation
        self.lease(engine, generation=new_generation, ready=True)
        self.tick(engine)
        self.assertEqual(engine.state.applied, TENT)
        self.assertIsNone(engine.state.error_code)

    def test_retry_unsupported_model_raises(self) -> None:
        profile = panasonic_profile()
        profile.panasonic = False
        engine = self.make(profile=profile)
        with self.assertRaises(ServiceError) as caught:
            engine.retry()
        self.assertEqual(caught.exception.code, ErrorCode.UNSUPPORTED_MODEL)

    def test_retry_in_development_does_not_touch_actuator(self) -> None:
        engine = self.make(development=True)
        generation = self.controller.generation
        self.assertTrue(engine.retry())
        self.assertEqual(self.controller.generation, generation + 1)
        self.assertNotIn("retry", self.actuator.calls)


class SleepEventTests(EngineTestCase):
    def test_suspend_releases_actuator_and_resets_decisions(self) -> None:
        engine = self.make()
        self.lease(engine)
        engine.agent_ready = self.controller.generation
        generation = self.controller.generation
        engine.sleep_event(True)
        self.assertTrue(engine.suspended)
        self.assertEqual(self.actuator.calls.count("release"), 1)
        self.assertEqual(self.controller.reset_calls, 1)
        self.assertEqual(self.controller.generation, generation + 1)
        self.assertIsNone(engine.agent_uid)
        self.assertIsNone(engine.agent_ready)
        self.assertIsNone(engine._hardware_monitor.last_valid)
        self.tick(engine)
        self.assertFalse(engine.state.session_connected)

    def test_resume_resets_again(self) -> None:
        engine = self.make()
        self.lease(engine)
        engine.sleep_event(True)
        engine.sleep_event(False)
        self.assertFalse(engine.suspended)
        self.assertEqual(self.controller.reset_calls, 2)
        self.assertEqual(self.controller.manual, Mode.AUTO)


class TickRobustnessTests(EngineTestCase):
    def test_sensor_exception_does_not_break_tick(self) -> None:
        engine = self.make()
        self.sensor.reading_error = RuntimeError("sensor exploded")
        self.tick(engine)  # must not raise
        self.assertEqual(engine.state.error_code, ErrorCode.INTERNAL)
        self.sensor.reading_error = None
        self.tick(engine)
        self.assertIsNone(engine.state.error_code)

    def test_controller_exception_does_not_break_tick(self) -> None:
        engine = self.make()
        self.controller.update_error = RuntimeError("controller exploded")
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.INTERNAL)
        self.controller.update_error = None
        self.tick(engine)
        self.assertIsNone(engine.state.error_code)

    def test_slow_tick_logs_warning(self) -> None:
        clock = FakeClock(100.0, step=1.0)
        engine = self.make(monotonic=clock, logger=mock.Mock())
        self.tick(engine)
        engine._log.warning.assert_called()  # type: ignore[attr-defined]

    def test_guard_contains_callback_exceptions(self) -> None:
        with mock.patch("panasonic_posture.service.engine.LOG") as log:
            callback = mock.Mock(side_effect=RuntimeError("boom"))
            wrapped = guard(callback)
            self.assertTrue(wrapped())  # GLib.SOURCE_CONTINUE
            callback.assert_called_once()
            log.exception.assert_called_once()
            self.assertEqual(guard(lambda: "value")(), "value")
            self.assertEqual(guard(lambda: 1 / 0, default=False)(), False)  # type: ignore[operator]

    def test_start_and_shutdown(self) -> None:
        engine = self.make()
        engine.start()
        self.assertEqual(self.sensor.started, 1)
        engine.shutdown()
        self.assertIn("release", self.actuator.calls)


class PresentationTests(EngineTestCase):
    def _valid_reading(self, stamp: float | None = None) -> SensorReading:
        return SensorReading((0.0, 0.0, -9.81), self.boot.value if stamp is None else stamp, SensorSource.IIO)

    def test_candidate_maps_policy_mode(self) -> None:
        engine = self.make()
        self.controller.policy.mode = "T"
        self.controller.policy.stable = True
        self.sensor._reading = self._valid_reading()
        self.tick(engine)
        self.assertEqual(self.presentation.calls[-1][1], Pose.TENT)
        for mode, pose in (("S", Pose.STAND), ("L", Pose.LAPTOP), ("P", Pose.TENT)):
            self.controller.policy.mode = mode
            self.tick(engine)
            self.assertEqual(self.presentation.calls[-1][1], pose)

    def test_hardware_tablet_forces_tablet_pose(self) -> None:
        engine = self.make()
        self.hardware = True
        self.tick(engine)
        self.assertEqual(engine.state.pose.value, "tablet")

    def test_stale_sensor_clears_presentation_stamp(self) -> None:
        engine = self.make()
        self.controller.policy.mode = "T"
        self.controller.policy.stable = True
        self.sensor._reading = SensorReading((0.0, 0.0, -9.81), 900.0, SensorSource.IIO)
        self.tick(engine)
        self.assertIsNone(self.presentation.calls[-1][3])
        self.assertEqual(self.presentation.calls[-1][1], Pose.UNKNOWN)

    def test_presentation_exception_is_contained(self) -> None:
        self.presentation.update = mock.Mock(side_effect=RuntimeError("drawing failed"))
        engine = self.make()
        self.tick(engine)  # must not raise
        self.assertEqual(engine.state.pose.value, "unknown")


class RealDomainIntegrationTests(unittest.TestCase):
    """Smoke test with the real policy/controller/presentation (no fakes)."""

    def test_one_tick_with_real_domain_objects(self) -> None:
        from panasonic_posture.controller import Controller
        from panasonic_posture.policy import Policy
        from panasonic_posture.presentation import PosePresentation

        controller = Controller(Policy())
        controller.generation = 5
        sensor = FakeSensor()
        sensor._reading = SensorReading((0.0, 0.0, -9.81), 1000.0, SensorSource.IIO)
        sensor.source = SensorSource.IIO
        boot = FakeClock(1000.0)
        mono = FakeClock(100.0)
        hardware = [True]
        engine = Engine(
            controller=controller,
            sensor=sensor,
            actuator=FakeActuator(),
            access=FakeAccess(),
            presentation=PosePresentation(),
            settings=FakeSettings(),
            profile=panasonic_profile(),
            hardware_query=lambda: (hardware[0], HardwareStatus.OK),
            instance="integration",
            monotonic=mono,
            clock=boot,
            logger=mock.Mock(),
        )
        engine.tick(mono.value)
        state = engine.state
        self.assertEqual(engine.state.to_dict()["version"], 2)
        self.assertEqual(state.target.mode, "hardware")
        self.assertEqual(state.pose.value, "tablet")
        self.assertTrue(state.supported)
        hardware[0] = False
        engine.tick(mono.value)
        self.assertNotEqual(engine.state.target.mode, "hardware")


if __name__ == "__main__":
    unittest.main()
