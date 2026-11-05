"""Tests for :mod:`panasonic_posture.service.engine` (all dependencies faked)."""

from __future__ import annotations

import json
import math
from dataclasses import replace
import unittest
from concurrent.futures import Executor, Future
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
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.model import SensorInfo, DeviceInfo
from panasonic_posture.service.ports import SessionOwner
from panasonic_posture.service.settings import Settings
from panasonic_posture.session.runtime import SessionRuntime

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


class DeferredExecutor(Executor):
    """Hold submitted work so tests can observe the non-blocking tick."""

    def __init__(self) -> None:
        self.calls = []

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        self.calls.append((future, fn, args, kwargs))
        return future

    def run_next(self) -> None:
        future, fn, args, kwargs = self.calls.pop(0)
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        del wait, cancel_futures


class FakeController:
    """Mirror of the real Controller interface owned by the core-domain task."""

    def __init__(self) -> None:
        self.policy = SimpleNamespace(mode="D", stable=False)
        self.manual = Mode.AUTO
        self.tent_enabled = True
        self.tent_prompt = True
        self.stand_prompt = False
        self.target = Target()
        self.generation = 7
        self.applied: Target | None = None
        self.error_code: str | None = None
        self.error_detail: str | None = None
        self.hardware: bool | None = None
        self.pending_epoch: int | None = None
        self.pending_kind: str | None = None
        self.update_error: Exception | None = None
        self.fail_ack = False
        self.update_calls: list = []
        self.acknowledge_calls: list = []
        self.set_manual_calls: list = []
        self.confirm_calls: list = []
        self.stand_confirm_calls: list = []
        self.confirm_result = True
        self.stand_confirm_result = True
        self.reset_calls = 0

    @property
    def pending_notification(self) -> int | None:
        return self.pending_epoch

    @property
    def pending_prompt(self) -> str | None:
        return self.pending_kind

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

    def confirm_stand(self, epoch) -> bool:
        self.stand_confirm_calls.append(epoch)
        return self.stand_confirm_result

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

    @property
    def info(self):
        return SensorInfo(ready=self._reading.valid, backend="" if self.source == "starting" else str(self.source),
                          error=self.sensor_error)

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
    def __init__(
        self,
        tent_prompt: bool = True,
        stand_prompt: bool = False,
        tent_enabled: bool = True,
    ) -> None:
        self.value = Settings(
            tent_enabled=tent_enabled,
            tent_prompt=tent_prompt,
            stand_prompt=stand_prompt,
        )
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


def validated_profile() -> DeviceInfo:
    return DeviceInfo(support_tier="validated", id="cfqv8", product="CFQV8",
                      vendor="Panasonic Corporation", platform="linux")


class EngineTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.mono = FakeClock(100.0)
        self.boot = FakeClock(1000.0)
        self.profile = validated_profile()
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
            presentation=self.presentation,
            settings=self.settings,
            device=self.profile,
            hardware_query=self.query,
            instance="0123456789abcdef",
            development=False,
            stale_seconds=2.0,
            monotonic=self.mono,
            clock=self.boot,
            on_state=self.listener,
            logger=mock.Mock(),
            executor=InlineExecutor(),
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
            SessionOwner("linux-uid", str(uid)),
        )


class StateSnapshotTests(EngineTestCase):
    def test_posture_wait_is_not_published_without_healthy_service_evidence(self):
        engine = self.make()
        self.controller.policy.posture_pending = True
        self.sensor._reading = SensorReading((0.0, 0.0, -9.81), self.boot.value, SensorSource.SYSFS)
        self.lease(engine)
        self.tick(engine)
        self.assertTrue(engine.state.posture_pending)
        self.sensor.sensor_error = "lost sensor"
        self.tick(engine)
        self.assertFalse(engine.state.posture_pending)
        self.sensor.sensor_error = None
        self.controller.error_code = ErrorCode.RELAY_INPUT
        self.tick(engine)
        self.assertFalse(engine.state.posture_pending)
        self.controller.error_code = None
        self.mono.value += 10.0
        self.tick(engine)
        self.assertFalse(engine.state.posture_pending)

    def test_initial_state_uses_schema_version_two(self) -> None:
        engine = self.make()
        state = engine.state.to_dict()
        self.assertEqual(state["version"], 2)
        self.assertEqual(state["instance"], "0123456789abcdef")
        self.assertEqual(state["supported"], True)
        self.assertEqual(state["support_tier"], "validated")
        self.assertEqual(state["backend"], "ready")
        self.assertEqual(state["profile"], "cfqv8")
        self.assertEqual(state["model"], "CFQV8")
        self.assertEqual(state["vendor"], "Panasonic Corporation")
        self.assertEqual(state["sensor_source"], "starting")
        self.assertEqual(state["pose"], "laptop")
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
        profile = validated_profile()
        profile = replace(profile, support_tier="unsupported")
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

    def test_compatible_support_does_not_depend_on_the_vendor(self) -> None:
        profile = validated_profile()
        profile = replace(profile, support_tier="compatible", id="generic",
                          vendor="Another Vendor", product="Convertible-1")
        engine = self.make(device=profile)
        self.tick(engine)
        self.assertTrue(engine.state.supported)
        self.assertEqual(engine.state.support_tier, "compatible")
        self.assertEqual(engine.state.backend.value, "ready")

    def test_settings_apply_tent_prompt_at_construction(self) -> None:
        self.settings = FakeSettings(tent_prompt=False)
        engine = self.make()
        self.assertFalse(engine.state.tent_prompt)
        self.assertFalse(self.controller.tent_prompt)

    def test_settings_apply_tent_enabled_at_construction(self) -> None:
        self.settings = FakeSettings(tent_enabled=False)
        engine = self.make()
        self.assertFalse(engine.state.tent_enabled)
        self.assertFalse(self.controller.tent_enabled)

    def test_settings_apply_stand_prompt_at_construction(self) -> None:
        self.settings = FakeSettings(stand_prompt=True)
        engine = self.make()
        self.assertTrue(engine.state.stand_prompt)
        self.assertTrue(self.controller.stand_prompt)

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

    def test_prefer_shared_keeps_sysfs_during_stand_prompt(self) -> None:
        engine = self.make()
        self.controller.policy.mode = "Q"
        self.controller.target = LAPTOP
        self.tick(engine)
        self.assertEqual(self.sensor.prefer_shared_calls, [True])


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

    def test_healthy_report_can_recover_from_a_transient_input_grab_error(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.lease(engine)
        self.actuator.keep_state = RelayState(True, False, False, True, "input-grab-failed")
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.RELAY_INPUT)
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.tick(engine)
        self.assertIsNone(engine.state.error_code)
        self.assertEqual(engine.state.applied, TENT)

    def test_failed_keep_releases_asynchronously_and_publishes_error(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.lease(engine)
        self.tick(engine)
        self.assertEqual(engine.state.applied, TENT)

        self.actuator.keep_error = ActuatorError(ErrorCode.RELAY_HEARTBEAT, "no reports")
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["keep", "keep"])
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(engine.state.error_code, ErrorCode.RELAY_HEARTBEAT)
        self.assertIsNone(engine.state.applied)

        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["keep", "keep"])
        self.assertEqual(len(executor.calls), 1)
        self.lease(engine, ready=False)
        self.tick(engine)
        self.assertTrue(engine.state.session_connected)
        executor.run_next()
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["keep", "keep", "release"])
        self.assertEqual(engine.state.error_code, ErrorCode.RELAY_HEARTBEAT)
        self.assertIsNone(engine.state.applied)

    def test_failed_keep_releases_even_after_same_target_was_released(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.tick(engine)
        executor.run_next()
        self.lease(engine)
        self.actuator.keep_error = ActuatorError(ErrorCode.RELAY_START, "cannot start")

        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["release", "keep"])
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(engine.state.error_code, ErrorCode.RELAY_START)

    def test_failed_keep_surfaces_native_recovery_failure(self) -> None:
        for deferred in (False, True):
            with self.subTest(deferred=deferred):
                self.setUp()
                executor = DeferredExecutor() if deferred else InlineExecutor()
                engine = self.make(executor=executor)
                self.controller.target = TENT
                self.lease(engine)
                self.actuator.keep_error = ActuatorError(ErrorCode.RELAY_HEARTBEAT, "no reports")

                def fail_recovery() -> None:
                    self.actuator.error_code = ErrorCode.NATIVE_RECOVERY
                    self.actuator.error_detail = "cannot restore native state"

                self.actuator.release_hook = fail_recovery
                self.tick(engine)
                if deferred:
                    self.assertEqual(engine.state.error_code, ErrorCode.RELAY_HEARTBEAT)
                    executor.run_next()
                    self.lease(engine, ready=False)
                    self.tick(engine)
                self.assertEqual(engine.state.error_code, ErrorCode.NATIVE_RECOVERY)
                self.assertEqual(engine.state.error_detail, "cannot restore native state")
                self.assertIsNone(engine.state.applied)

    def test_stale_failed_release_does_not_overwrite_new_generation(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.lease(engine)
        self.actuator.keep_error = ActuatorError(ErrorCode.RELAY_HEARTBEAT, "no reports")
        self.actuator.release_hook = mock.Mock(side_effect=RuntimeError("release failed"))
        self.tick(engine)

        engine.set_mode("laptop")
        self.controller.target = LAPTOP
        executor.run_next()
        self.tick(engine)
        self.assertIsNone(engine.state.error_code)
        self.assertIsNone(engine.state.applied)
        self.assertEqual(len(executor.calls), 1)

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
        # The lease expired while the inline release ran; the published state
        # must reflect the fresh clock, not the pre-release one.
        self.assertFalse(engine.state.session_connected)
        self.assertIsNone(engine.state.applied)

    def test_slow_release_does_not_block_the_tick(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = LAPTOP
        self.lease(engine)

        self.tick(engine)
        self.assertEqual(len(executor.calls), 1)
        self.assertNotIn("release", self.actuator.calls)
        self.assertIsNone(engine.state.applied)

        # A second tick observes the in-flight release without starting keep or
        # another release, then consumes the result once the worker finishes.
        self.tick(engine)
        self.assertEqual(len(executor.calls), 1)
        executor.run_next()
        self.tick(engine)
        self.assertIn("release", self.actuator.calls)
        self.assertEqual(self.controller.acknowledge_calls[-1][1], True)
        self.assertEqual(engine.state.applied, LAPTOP)

    def test_keep_waits_for_an_inflight_release(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = LAPTOP
        self.lease(engine)
        self.tick(engine)

        self.controller.target = TENT
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.tick(engine)
        self.assertEqual(self.actuator.calls, [])

        executor.run_next()
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["release", "keep"])
        self.assertEqual(engine.state.applied, TENT)

    def test_release_runs_again_after_a_keep_cycle(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.lease(engine)
        self.tick(engine, now=100.0)
        self.assertEqual(self.actuator.calls.count("release"), 0)

        self.tick(engine, now=103.5)  # lease expiry releases the relay
        self.assertEqual(self.actuator.calls.count("release"), 1)

        self.lease(engine)
        self.tick(engine, now=104.0)  # recovered lease keeps it again
        self.assertEqual(self.actuator.calls.count("keep"), 2)

        self.tick(engine, now=107.5)  # the next expiry must release again
        self.assertEqual(self.actuator.calls.count("release"), 2)

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

    def test_released_target_accepts_late_ready_and_clears_on_lease_expiry(self) -> None:
        for target, hardware in ((LAPTOP, False), (HARDWARE, True)):
            with self.subTest(mode=target.mode):
                self.setUp()
                engine = self.make()
                self.controller.target = target
                self.hardware = hardware
                self.tick(engine, now=100.0)
                self.assertIsNone(engine.state.applied)

                self.lease(engine, generation=self.controller.generation - 1)
                self.tick(engine, now=100.1)
                self.assertIsNone(engine.state.applied)
                self.lease(engine)
                self.tick(engine, now=100.2)
                self.assertEqual(engine.state.applied, target)

                self.tick(engine, now=103.5)
                self.assertFalse(engine.state.session_connected)
                self.assertIsNone(engine.state.applied)
                self.lease(engine)
                self.tick(engine)
                self.assertEqual(engine.state.applied, target)
                self.assertEqual(self.actuator.calls, ["release"])

    def test_software_target_waits_for_keep_after_release(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.tick(engine)
        self.lease(engine)
        executor.run_next()
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["release", "keep"])
        self.assertIsNone(engine.state.applied)

    def test_switch_between_software_orientations_keeps_relay(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        # Lease is present but the display prepared an older generation.
        engine.session_owner = SessionOwner("linux-uid", "1000")
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

    def test_opaque_owners_cannot_refresh_or_steal_each_others_lease(self):
        engine = self.make()
        owner = SessionOwner("platform-a", "user/session")
        other = SessionOwner("platform-b", "user/session")
        generation = self.controller.generation
        engine.session_update(generation, True, None, None, owner)
        self.mono.value += 1
        with self.assertRaises(ServiceError):
            engine.session_update(generation, True, None, None, other)
        self.assertEqual(engine.session_owner, owner)
        self.assertEqual(engine.agent_seen, 100)
        self.assertFalse(engine.session_update(generation - 1, True, None, None, owner))
        self.assertEqual(engine.agent_seen, 100)
        self.mono.value += 3
        self.assertTrue(engine.session_update(generation, True, None, None, other))
        self.assertEqual(engine.session_owner, other)

    def test_invalid_owner_never_refreshes_lease(self):
        engine = self.make()
        with self.assertRaises(TypeError):
            engine.session_update(self.controller.generation, True, None, None, None)
        self.assertIsNone(engine.session_owner)
        self.assertEqual(engine.agent_seen, float("-inf"))

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
        self.assertNotIn("retry", self.actuator.calls)

    def test_set_mode_keeps_a_healthy_relay_running(self) -> None:
        engine = self.make()
        self.actuator.running = True
        self.actuator.state = RelayState(True, True, False, True, None)
        self.assertTrue(engine.set_mode("tablet"))
        self.assertNotIn("retry", self.actuator.calls)
        self.assertTrue(self.actuator.running)

    def test_set_mode_resets_only_a_failed_relay(self) -> None:
        engine = self.make()
        self.actuator.error_code = ErrorCode.RELAY_EXITED
        self.actuator.error_detail = "relay exited with status 3"
        self.assertTrue(engine.set_mode("tablet"))
        self.assertIn("retry", self.actuator.calls)
        self.assertIsNone(self.actuator.error_code)

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

    def test_set_tent_enabled_persists_and_is_idempotent(self) -> None:
        engine = self.make()
        self.assertTrue(engine.set_tent_enabled(False))
        self.assertEqual(self.settings.saved, [Settings(tent_enabled=False)])
        self.assertFalse(self.controller.tent_enabled)
        self.assertFalse(engine.set_tent_enabled(False))
        self.assertEqual(len(self.settings.saved), 1)

    def test_set_tent_enabled_write_failure_keeps_old_value(self) -> None:
        engine = self.make()
        self.settings.fail = True
        with self.assertRaises(ServiceError) as caught:
            engine.set_tent_enabled(False)
        self.assertEqual(caught.exception.code, ErrorCode.CONFIG_WRITE)
        self.assertTrue(self.controller.tent_enabled)
        self.assertTrue(engine.state.tent_enabled)

    def test_set_tent_prompt_write_failure_keeps_old_value(self) -> None:
        engine = self.make()
        self.settings.fail = True
        with self.assertRaises(ServiceError) as caught:
            engine.set_tent_prompt(False)
        self.assertEqual(caught.exception.code, ErrorCode.CONFIG_WRITE)
        self.assertTrue(self.controller.tent_prompt)
        self.assertTrue(engine.state.tent_prompt)

    def test_set_stand_prompt_persists_and_preserves_tent_prompt(self) -> None:
        engine = self.make()
        self.assertTrue(engine.set_stand_prompt(True))
        self.assertEqual(
            self.settings.saved,
            [Settings(tent_prompt=True, stand_prompt=True)],
        )
        self.assertTrue(self.controller.stand_prompt)
        self.assertFalse(engine.set_stand_prompt(True))
        self.assertEqual(len(self.settings.saved), 1)
        self.assertTrue(engine.set_tent_prompt(False))
        self.assertEqual(
            self.settings.saved[-1],
            Settings(tent_prompt=False, stand_prompt=True),
        )

    def test_set_stand_prompt_write_failure_keeps_old_value(self) -> None:
        engine = self.make()
        self.settings.fail = True
        with self.assertRaises(ServiceError) as caught:
            engine.set_stand_prompt(True)
        self.assertEqual(caught.exception.code, ErrorCode.CONFIG_WRITE)
        self.assertFalse(self.controller.stand_prompt)
        self.assertFalse(engine.state.stand_prompt)

    def test_confirm_tent_success_and_expiry(self) -> None:
        engine = self.make()
        self.controller.confirm_result = True
        self.assertTrue(engine.confirm_tent(3))
        self.assertEqual(self.controller.confirm_calls[-1], 3)
        self.controller.confirm_result = False
        with self.assertRaises(ServiceError) as caught:
            engine.confirm_tent(4)
        self.assertEqual(caught.exception.code, ErrorCode.EXPIRED_CONFIRMATION)

    def test_confirm_stand_success_and_expiry(self) -> None:
        engine = self.make()
        self.controller.stand_confirm_result = True
        self.assertTrue(engine.confirm_stand(3))
        self.assertEqual(self.controller.stand_confirm_calls[-1], 3)
        self.controller.stand_confirm_result = False
        with self.assertRaises(ServiceError) as caught:
            engine.confirm_stand(4)
        self.assertEqual(caught.exception.code, ErrorCode.EXPIRED_CONFIRMATION)


class RetryTests(EngineTestCase):
    def test_retry_waits_for_release_on_worker_without_blocking_method(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.tick(engine)
        release = engine._release_task.future
        generation = self.controller.generation
        with mock.patch.object(release, "result", side_effect=RuntimeError("would block")) as wait:
            self.assertTrue(engine.retry())
            wait.assert_not_called()
        self.assertEqual(self.actuator.calls, [])
        self.assertEqual(self.controller.generation, generation + 1)
        self.assertFalse(self.lease(engine, generation=generation))
        executor.run_next()
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["release"])
        self.assertEqual(len(executor.calls), 1)
        self.lease(engine)
        self.tick(engine)
        self.assertNotIn("keep", self.actuator.calls)
        executor.run_next()
        self.tick(engine)
        self.assertIn("retry", self.actuator.calls)
        self.assertIsNone(engine.state.applied)

    def test_retry_reset_runs_on_worker_and_repeated_requests_are_coalesced(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.actuator.error_code = ErrorCode.RELAY_EXITED
        self.assertTrue(engine.retry())
        self.assertTrue(engine.retry())
        self.assertEqual(self.actuator.calls, [])
        self.assertEqual(len(executor.calls), 1)
        self.tick(engine)
        self.assertIsNone(engine.state.error_code)
        self.assertIsNone(engine.state.applied)
        self.assertEqual(self.actuator.error_code, ErrorCode.RELAY_EXITED)
        executor.run_next()
        self.lease(engine)
        self.tick(engine)
        self.assertEqual(self.actuator.calls.count("retry"), 1)

    def test_reset_failure_is_published_without_confirming_target(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.actuator.retry = mock.Mock(side_effect=ActuatorError(ErrorCode.NATIVE_RECOVERY, "failed"))
        engine.retry()
        engine.retry()  # coalesce onto the reset, retaining the newest generation
        self.lease(engine)  # display can finish before the input reset does
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        executor.run_next()
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.NATIVE_RECOVERY)
        self.assertIsNone(engine.state.applied)
        self.assertNotIn("keep", self.actuator.calls)

    def test_mode_change_with_failed_actuator_schedules_reset(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.actuator.error_code = ErrorCode.RELAY_EXITED
        self.assertTrue(engine.set_mode("tent"))
        self.assertEqual(self.actuator.calls, [])
        self.assertEqual(len(executor.calls), 1)
        self.tick(engine)
        self.assertNotIn("keep", self.actuator.calls)
        executor.run_next()
        self.tick(engine)
        self.assertEqual(self.actuator.calls.count("retry"), 1)

    def test_mode_change_cannot_hide_a_running_reset_failure(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.actuator.retry = mock.Mock(side_effect=RuntimeError("reset failed"))
        engine.retry()
        engine.set_mode("stand")
        self.controller.target = STAND
        self.lease(engine)
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        executor.run_next()
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.NATIVE_RECOVERY)
        self.assertIsNone(engine.state.applied)
        self.assertNotIn("keep", self.actuator.calls)

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
        profile = validated_profile()
        profile = replace(profile, support_tier="unsupported")
        engine = self.make(device=profile)
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
        self.assertIsNone(engine.session_owner)
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
    def test_failed_tick_revokes_takeover_before_accepting_fresh_readiness(self) -> None:
        executor = DeferredExecutor()
        engine = self.make(executor=executor)
        self.controller.target = TENT
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        generation = self.controller.generation
        self.lease(engine)
        self.tick(engine)
        self.assertEqual(engine.state.applied, TENT)

        self.controller.update_error = RuntimeError("controller exploded")
        self.tick(engine)
        self.assertEqual(engine.state.error_code, ErrorCode.INTERNAL)
        self.assertIsNone(engine.state.applied)
        self.assertFalse(self.lease(engine, generation=generation))
        fault_generation = engine.state.generation
        self.tick(engine)
        self.assertEqual(engine.state.generation, fault_generation)
        self.assertEqual(len(executor.calls), 1)

        self.controller.update_error = None
        self.lease(engine)
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["keep"])
        self.assertIsNone(engine.state.applied)
        executor.run_next()
        self.tick(engine)
        self.assertEqual(self.actuator.calls, ["keep", "release", "keep"])
        self.assertEqual(engine.state.applied, TENT)

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


class SensorCalibrationTests(EngineTestCase):
    def feed(self, engine, vector, count=3, start=1000.0):
        for index in range(count):
            stamp = start + index * 0.1
            self.boot.value = stamp
            self.sensor._reading = SensorReading(vector, stamp, SensorSource.IIO)
            self.tick(engine)

    def test_calibration_persists_and_corrects_following_samples(self):
        self.settings.value = Settings(tent_prompt=False, stand_prompt=True)
        engine = self.make()
        self.feed(engine, (2.0, 0.0, -9.8))

        self.assertTrue(engine.calibrate_sensor())
        saved = self.settings.saved[-1]
        self.assertFalse(saved.tent_prompt)
        self.assertTrue(saved.stand_prompt)
        self.assertIsNotNone(saved.sensor_calibration)

        self.tick(engine)
        self.assertTrue(engine.state.sensor_calibrated)
        corrected = self.controller.update_calls[-1][1]
        self.assertAlmostEqual(corrected.vector[0], 0.0, places=5)
        self.assertAlmostEqual(corrected.vector[1], 0.0, places=5)
        self.assertAlmostEqual(corrected.vector[2], -(4.0 + 9.8 ** 2) ** 0.5, places=6)

    def test_loaded_calibration_is_reported_in_state(self):
        identity = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        self.settings.value = Settings(sensor_calibration=identity)
        engine = self.make()
        self.assertTrue(engine.state.sensor_calibrated)

    def test_reset_removes_calibration_and_preserves_prompts(self):
        self.settings.value = Settings(tent_prompt=False, stand_prompt=True)
        engine = self.make()
        self.feed(engine, (2.0, 0.0, -9.8))
        self.assertTrue(engine.calibrate_sensor())
        self.assertTrue(engine.reset_sensor_calibration())

        saved = self.settings.saved[-1]
        self.assertIsNone(saved.sensor_calibration)
        self.assertFalse(saved.tent_prompt)
        self.assertTrue(saved.stand_prompt)
        self.tick(engine)
        self.assertFalse(engine.state.sensor_calibrated)
        self.assertFalse(engine.reset_sensor_calibration())

    def test_requires_enough_stable_flat_samples(self):
        engine = self.make()
        self.feed(engine, (0.0, 0.0, -9.8), count=1)
        with self.assertRaises(ServiceError) as caught:
            engine.calibrate_sensor()
        self.assertEqual(caught.exception.code, ErrorCode.CALIBRATION_NOT_READY)

        self.feed(engine, (0.0, 0.0, 9.8), count=3, start=1000.3)
        with self.assertRaises(ServiceError) as caught:
            engine.calibrate_sensor()
        self.assertEqual(caught.exception.code, ErrorCode.CALIBRATION_NOT_READY)

    def test_rejects_calibration_while_hardware_tablet_mode_is_active(self):
        self.hardware = True
        engine = self.make()
        self.feed(engine, (0.0, 0.0, -9.8))
        with self.assertRaises(ServiceError) as caught:
            engine.calibrate_sensor()
        self.assertEqual(caught.exception.code, ErrorCode.CALIBRATION_NOT_READY)

    def test_settings_failure_keeps_previous_calibration(self):
        engine = self.make()
        self.feed(engine, (2.0, 0.0, -9.8))
        self.settings.fail = True
        with self.assertRaises(ServiceError) as caught:
            engine.calibrate_sensor()
        self.assertEqual(caught.exception.code, ErrorCode.CONFIG_WRITE)
        self.assertFalse(engine.state.sensor_calibrated)

    def test_slow_tick_logs_warning(self) -> None:
        clock = FakeClock(100.0, step=1.0)
        engine = self.make(monotonic=clock, logger=mock.Mock())
        self.tick(engine)
        engine._log.warning.assert_called()  # type: ignore[attr-defined]

    def test_guard_contains_callback_exceptions(self) -> None:
        with mock.patch("panasonic_posture.service.callbacks.LOG") as log:
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
        for mode, pose in (
            ("Q", Pose.STAND),
            ("S", Pose.STAND),
            ("L", Pose.LAPTOP),
            ("P", Pose.TENT),
        ):
            self.controller.policy.mode = mode
            self.tick(engine)
            self.assertEqual(self.presentation.calls[-1][1], pose)

    def test_manual_target_does_not_replace_recognised_pose(self) -> None:
        engine = self.make()
        self.controller.policy.mode = "T"
        self.controller.policy.stable = True
        self.controller.manual = Mode.TABLET
        self.controller.target = Target(
            mode="tablet",
            tablet=True,
            rotation=None,
            disable_internal_input=True,
        )
        self.sensor._reading = SensorReading(
            (0.0, -9.81, -1.73), self.boot.value, SensorSource.IIO
        )
        self.tick(engine)
        self.assertEqual(engine.state.manual, Mode.TABLET)
        self.assertEqual(engine.state.target.mode, "tablet")
        self.assertEqual(engine.state.pose, Pose.TENT)

    def test_hardware_tablet_forces_tablet_pose(self) -> None:
        engine = self.make()
        self.hardware = True
        self.tick(engine)
        self.assertEqual(engine.state.pose.value, "tablet")

    def test_real_hardware_to_stand_handoff_never_publishes_laptop(self) -> None:
        from panasonic_posture.controller import Controller
        from panasonic_posture.presentation import PosePresentation

        self.controller = Controller()
        self.presentation = PosePresentation()
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        engine = self.make()
        self.hardware = True
        self.sensor._reading = SensorReading(
            (0.0, 0.0, 9.81), self.boot.value, SensorSource.IIO
        )
        self.tick(engine)
        self.lease(engine)
        self.tick(engine)
        self.assertEqual(engine.state.applied, HARDWARE)

        self.hardware = False
        for _ in range(20):
            self.boot.value += 0.1
            self.mono.value += 0.1
            self.sensor._reading = SensorReading(
                (0.0, -0.85, 9.77), self.boot.value, SensorSource.IIO
            )
            self.tick(engine)
            self.assertIs(engine.state.hardware, False)
            self.assertIn(engine.state.target.mode, ("unavailable", "stand"))
            self.assertIn(engine.state.pose, (Pose.TABLET, Pose.STAND))
            if engine.state.target == STAND:
                self.assertFalse(engine.state.posture_pending)
                self.lease(engine)
                self.tick(engine)
            else:
                self.assertTrue(engine.state.posture_pending)
                self.assertIsNone(engine.state.applied)

        self.assertEqual(engine.state.pose, Pose.STAND)
        self.assertEqual(engine.state.applied, STAND)
        self.assertTrue(self.actuator.running)

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
        self.assertEqual(engine.state.pose.value, "laptop")

    def test_sample_published_during_read_is_not_treated_as_future(self) -> None:
        """A sample acquired while the tick starts must remain displayable.

        The boot clock advances between the old pre-read timestamp and the
        sensor snapshot.  Comparing that earlier timestamp with the newer
        sample made the sample look like it came from the future, which reset
        the stable run and briefly flashed the unknown presentation state.
        """
        engine = self.make()
        self.controller.policy.mode = "L"
        self.controller.policy.stable = True

        def reading_during_clock_advance():
            self.boot.value += 0.001
            stamp = self.boot.value
            self.sensor._reading = SensorReading(
                (0.0, 0.0, -9.81), stamp, SensorSource.IIO
            )
            return self.sensor._reading

        self.sensor.reading = reading_during_clock_advance
        self.tick(engine)

        self.assertEqual(engine.state.pose, Pose.LAPTOP)
        self.assertEqual(self.presentation.calls[-1][3], 1000.001)


class SoftwarePostureIntegrationTests(EngineTestCase):
    def test_slow_samples_keep_input_through_both_orientation_changes(self) -> None:
        from panasonic_posture.controller import Controller
        from panasonic_posture.presentation import PosePresentation

        self.controller = Controller()
        self.presentation = PosePresentation()
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        engine = self.make()

        def step(pitch):
            self.boot.value += 0.6
            self.mono.value += 0.6
            angle = math.radians(pitch)
            self.sensor._reading = SensorReading(
                (0.0, -9.81 * math.sin(angle), -9.81 * math.cos(angle)),
                self.boot.value, SensorSource.SYSFS,
            )
            self.lease(engine)
            self.tick(engine)

        for _ in range(8):
            step(80.0)
        self.assertEqual(engine.state.applied, TENT)
        releases = self.actuator.calls.count("release")
        preferences = list(self.sensor.prefer_shared_calls)
        previous = TENT
        for path, target in (
            ([120.0, 135.0, 150.0, 165.0] + [175.0] * 8, STAND),
            ([125.0, 110.0, 125.0, 110.0] + [80.0] * 8, TENT),
        ):
            old_generation = engine.state.generation
            for pitch in path:
                step(pitch)
                self.assertIn(engine.state.target, (TENT, STAND))
                self.assertEqual(self.actuator.calls.count("release"), releases)
                self.assertTrue(self.actuator.running)
                if engine.state.target != previous:
                    self.assertIsNone(engine.state.applied)
                    self.assertFalse(self.lease(engine, generation=old_generation))
                    previous = engine.state.target
            self.assertEqual(engine.state.applied, target)
            self.assertEqual(engine.state.generation, old_generation + 1)
        self.assertEqual(self.sensor.prefer_shared_calls, preferences)

        # A failure in the new display transaction must still revoke input.
        self.lease(engine, ready=False, code=ErrorCode.DISPLAY_UNAVAILABLE,
                   detail="display disappeared")
        self.tick(engine)
        self.tick(engine)
        self.assertIsNone(engine.state.applied)
        self.assertFalse(self.actuator.running)
        self.assertGreater(self.actuator.calls.count("release"), releases)


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
            presentation=PosePresentation(),
            settings=FakeSettings(),
            device=validated_profile(),
            hardware_query=lambda: (hardware[0], HardwareStatus.OK),
            instance="integration",
            monotonic=mono,
            clock=boot,
            logger=mock.Mock(),
            executor=InlineExecutor(),
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


class SlowDisplayIntegrationTests(EngineTestCase):
    def test_orientation_change_keeps_input_while_display_exceeds_lease_duration(self) -> None:
        engine = self.make()
        self.controller.target = TENT
        self.actuator.keep_state = RelayState(True, True, False, True, None)
        self.lease(engine)
        self.tick(engine)
        self.assertEqual(engine.state.applied, TENT)
        executor = DeferredExecutor()
        display = mock.Mock()
        runtime = SessionRuntime(
            SimpleNamespace(
                get_state=lambda: engine.state,
                session_update=lambda *args: engine.session_update(*args, owner=SessionOwner("linux-uid", "1000")),
            ),
            display, None, executor=executor, lease_executor=InlineExecutor(),
        )
        runtime.tick(100.0)
        executor.run_next()
        runtime.tick(100.1)
        engine.set_mode("stand")
        self.controller.target = STAND
        self.tick(engine, now=100.2)
        for now in (100.7, 101.3, 101.9, 102.5, 103.1, 103.7):
            self.mono.value = now
            runtime.tick(now)
            self.tick(engine)
            self.assertTrue(self.actuator.running)
            self.assertTrue(engine.state.session_connected)
            self.assertIsNone(engine.state.applied)
        for _ in range(8):
            if not executor.calls:
                break
            executor.run_next()
            runtime.drain_completed_tasks(103.8)
        self.tick(engine)
        self.assertEqual(engine.state.applied, STAND)
        self.assertEqual(self.actuator.calls.count("release"), 0)
        self.assertEqual(display.apply.call_args.args, (STAND,))
        runtime.close()


if __name__ == "__main__":
    unittest.main()
