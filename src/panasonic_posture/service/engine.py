"""Transaction layer between a service host and its platform adapters.

The engine is the only component that mutates the domain state.  Every tick it

1. reads the latest sensor sample and hardware switch state,
2. debounces transient ``hardware is None`` readings,
3. asks the (pure) controller for the target of the current generation,
4. checks the session lease and drives the input actuator (releases are
   serialized on a worker so a slow backend cannot stall the tick),
5. updates the display presentation and builds the public state snapshot.

Everything is injected (controller, sensor, actuator, access, presentation,
settings, machine identity, clocks, hardware query, state listener) so the complete
transaction can be unit tested without root, D-Bus, a relay binary or hardware.
"""

from __future__ import annotations

import logging
import secrets
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from typing import Any

from ..calibration import (
    CALIBRATION_MAX_FLAT_DEGREES,
    CALIBRATION_WINDOW_SECONDS,
    apply_calibration,
    calibration_matrix_from_gravity,
    parse_calibration_matrix,
    stable_gravity_vector,
)
from ..model import (
    BackendState,
    HardwareStatus,
    Mode,
    Pose,
    SensorReading,
    DeviceInfo,
    StateSnapshot,
    Target,
    dumps_state,
)
from ..contracts import (
    ALL_ERROR_CODES,
    HARDWARE_HANDOFF_SECONDS,
    LEASE_SECONDS,
    MODES,
    SLOW_TICK_WARN_SECONDS,
    SUPPORT_TIERS,
    ErrorCode,
    error_message,
)
from ..policy import geometry
from ..tasks import thread_executor
from .hardware_monitor import HardwareMonitor, HardwareObservation
from .ports import (
    ActuatorError, InputActuator, SensorBackend, Settings, SettingsBackend, SessionOwner,
)

LOG = logging.getLogger(__name__)

#: Policy mode (``L/P/Q/T/S/H/D``) to displayed pose.
MODE_TO_POSE = {
    "L": Pose.LAPTOP,
    "P": Pose.TENT,
    "Q": Pose.STAND,
    "T": Pose.TENT,
    "S": Pose.STAND,
    "H": Pose.TABLET,
    "D": Pose.UNKNOWN,
}


class ServiceError(Exception):
    """A method failure carrying a stable :class:`~panasonic_posture.protocol.ErrorCode`."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = str(code or ErrorCode.INTERNAL)
        self.detail = detail
        super().__init__(error_message(self.code, detail))


@dataclass
class _ReleaseTask:
    future: Future[Any]
    generation: int
    retry: bool = False


class Engine:
    """Own the per-tick transaction and the public service state."""

    def __init__(
        self,
        *,
        controller: Any,
        sensor: SensorBackend,
        actuator: InputActuator,
        presentation: Any,
        settings: SettingsBackend | None,
        device: DeviceInfo,
        hardware_query: Callable[[], tuple[bool | None, Any]] | None = None,
        hardware_monitor: HardwareMonitor | None = None,
        instance: str | None = None,
        development: bool = False,
        stale_seconds: float = 2.0,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] | None = None,
        on_state: Callable[[StateSnapshot], None] | None = None,
        logger: logging.Logger = LOG,
        executor: Executor | None = None,
    ) -> None:
        self._controller = controller
        self._sensor = sensor
        self._actuator = actuator
        self._presentation = presentation
        self._settings = settings
        self._device = device
        if hardware_monitor is None:
            if hardware_query is None:
                raise TypeError("hardware_query or hardware_monitor is required")
            hardware_monitor = HardwareMonitor(hardware_query, logger=logger)
        self._hardware_monitor = hardware_monitor
        self._hardware_observation = HardwareObservation(
            None, None, HardwareStatus.ABSENT
        )
        self._instance = instance or secrets.token_hex(8)
        self.development = bool(development)
        self._stale_seconds = float(stale_seconds)
        self._monotonic = monotonic
        # Samples and calibration must share a clock. Lease deadlines use the
        # service's monotonic clock even when the sample clock counts suspend.
        self._clock = clock if clock is not None else monotonic
        self._on_state = on_state
        self._log = logger
        self._slow_tick_seconds = SLOW_TICK_WARN_SECONDS
        self._executor = executor if executor is not None else thread_executor(
            "posture-actuator"
        )
        self._owns_executor = executor is None
        self._release_task: _ReleaseTask | None = None
        self._retry_requested = False
        self._last_release_key: tuple[int, Target] | None = None
        self._following_hardware = False
        self._handoff_until: float | None = None

        support_tier = str(
            device.support_tier or "compatible"
        )
        if support_tier not in SUPPORT_TIERS:
            support_tier = "compatible"
        self.support_tier = support_tier
        self.supported = support_tier != "unsupported"
        self._manage_actuator = self.supported and not self.development

        # Sensor-source preference follows the effective hardware state.
        self._prefer_shared: bool | None = None

        # Identity is authenticated by the host before entering this engine.
        # The core only arbitrates ownership and checks lease deadlines.
        self.session_owner: SessionOwner | None = None
        self.agent_ready: int | None = None
        self.agent_error_code: str | None = None
        self.agent_error_detail: str | None = None
        self.agent_seen: float = float("-inf")
        self.suspended = False

        self._tick_error_code: str | None = None
        self._tick_error_detail: str | None = None
        self._pose = Pose.LAPTOP
        self._target = Target(mode="unavailable")
        self._sensor_calibration: tuple[float, ...] | None = None
        self._recent_raw_readings: deque[SensorReading] = deque(maxlen=128)

        self._load_settings()
        self._state = self._build_state(lease=False)
        self._state_json = dumps_state(self._state)

    # -- construction / lifecycle ---------------------------------------

    def _load_settings(self) -> None:
        load = getattr(self._settings, "load", None)
        if load is None:
            return
        try:
            settings = load()
        except Exception:  # noqa: BLE001 - a broken store must not stop the service
            self._log.exception("cannot load settings")
            return
        tent_enabled = getattr(settings, "tent_enabled", None)
        if isinstance(tent_enabled, bool):
            self._controller.tent_enabled = tent_enabled
        tent_prompt = getattr(settings, "tent_prompt", None)
        if isinstance(tent_prompt, bool):
            self._controller.tent_prompt = tent_prompt
        stand_prompt = getattr(settings, "stand_prompt", None)
        if isinstance(stand_prompt, bool):
            self._controller.stand_prompt = stand_prompt
        try:
            self._sensor_calibration = parse_calibration_matrix(
                getattr(settings, "sensor_calibration", None)
            )
        except ValueError:
            self._log.exception("cannot load sensor calibration")
            self._sensor_calibration = None

    def start(self) -> None:
        start = getattr(self._sensor, "start", None)
        if start is not None:
            start()

    def shutdown(self) -> None:
        """Release software control on shutdown (bounded by the actuator)."""
        self._retry_requested = False
        self._wait_for_release()
        try:
            self._actuator.release()
        except Exception:  # noqa: BLE001 - shutdown must always proceed
            self._log.exception("cannot release relay during shutdown")
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)

    @property
    def state(self) -> StateSnapshot:
        return self._state

    def state_json(self) -> str:
        return dumps_state(self._state)

    # -- D-Bus method handlers -------------------------------------------

    def set_mode(self, mode: str) -> bool:
        """Change the manual mode; idempotent when the mode is unchanged."""
        if not self.supported:
            raise ServiceError(ErrorCode.UNSUPPORTED_MODEL)
        value = str(mode)
        if value not in MODES:
            raise ServiceError(ErrorCode.INVALID_MODE, value)
        if self._hardware_monitor.last_valid is True and value != Mode.AUTO:
            raise ServiceError(ErrorCode.HARDWARE_PRIORITY, value)
        if str(self._controller.manual) == value:
            return False
        retry_actuator = self._manage_actuator and bool(
            getattr(self._actuator, "error_code", None)
        )
        try:
            self._controller.set_manual(value)
        except ValueError as error:
            # Controller.set_manual carries a stable ErrorCode as its message.
            message = str(error)
            code = (
                message
                if message in (ErrorCode.HARDWARE_PRIORITY, ErrorCode.INVALID_MODE)
                else ErrorCode.INVALID_MODE
            )
            raise ServiceError(code, message) from None
        self._bump_generation()
        # A healthy relay already represents "software tablet"; orientation and
        # lock changes are handled by the session display transaction.  Only a
        # latched relay failure needs a full reset here, otherwise switching
        # between tent/stand/tablet would release and re-grab the internal
        # input devices for no reason.
        if retry_actuator:
            self._queue_retry()
        self._log.info("manual mode changed to %s (generation %s)", value, self._controller.generation)
        return True

    def retry(self) -> bool:
        """Explicitly recover from a backend error (UI Retry / ``Retry()``).

        Clears the relay error latch, the controller/agent error state and
        bumps the generation so the display transaction is replayed on the
        next tick.  Unlike ``set_mode`` this is not idempotent: it is the
        dedicated recovery action for errors that ``SetMode`` cannot clear
        because the manual mode did not change (review Q-02).
        """
        if not self.supported:
            raise ServiceError(ErrorCode.UNSUPPORTED_MODEL)
        self._bump_generation()
        if self._manage_actuator:
            self._queue_retry()
        self._tick_error_code = None
        self._tick_error_detail = None
        self._log.info(
            "backend retry requested (generation %s)",
            getattr(self._controller, "generation", 0),
        )
        return True

    def set_tent_prompt(self, enabled: bool) -> bool:
        """Persist the tent prompt preference; idempotent and transactional."""
        value = bool(enabled)
        if bool(getattr(self._controller, "tent_prompt", True)) == value:
            return False
        self._save_prompt_settings(tent_prompt=value)
        self._controller.tent_prompt = value
        return True

    def set_tent_enabled(self, enabled: bool) -> bool:
        """Persist whether tent mode may be used; idempotent and transactional."""
        value = bool(enabled)
        if bool(getattr(self._controller, "tent_enabled", True)) == value:
            return False
        self._save_prompt_settings(tent_enabled=value)
        self._controller.tent_enabled = value
        self._log.info("tent mode %s", "enabled" if value else "disabled")
        return True

    def set_stand_prompt(self, enabled: bool) -> bool:
        """Persist the stand prompt preference; idempotent and transactional."""
        value = bool(enabled)
        if bool(getattr(self._controller, "stand_prompt", False)) == value:
            return False
        self._save_prompt_settings(stand_prompt=value)
        self._controller.stand_prompt = value
        return True

    def calibrate_sensor(self) -> bool:
        """Use the current flat laptop pose as the sensor's level reference."""

        if not self.supported:
            raise ServiceError(ErrorCode.UNSUPPORTED_MODEL)
        if self.suspended or self._hardware_observation.effective is True:
            raise ServiceError(ErrorCode.CALIBRATION_NOT_READY)

        now = self._clock()
        samples = [
            reading
            for reading in self._recent_raw_readings
            if reading.stamp is not None
            and -0.5 <= now - reading.stamp <= CALIBRATION_WINDOW_SECONDS
        ]
        try:
            gravity = stable_gravity_vector(samples)
            flat = geometry(gravity)[4]
        except ValueError:
            raise ServiceError(ErrorCode.CALIBRATION_NOT_READY) from None
        if flat is None or flat > CALIBRATION_MAX_FLAT_DEGREES:
            raise ServiceError(ErrorCode.CALIBRATION_NOT_READY)

        calibration = calibration_matrix_from_gravity(gravity)
        self._save_settings(
            self._make_settings(sensor_calibration=calibration)
        )
        self._sensor_calibration = calibration
        self._recent_raw_readings.clear()
        self._reset_after_calibration()
        self._log.info("accelerometer calibration updated")
        return True

    def reset_sensor_calibration(self) -> bool:
        """Remove the custom level reference and use the profile mount again."""

        if self._sensor_calibration is None:
            return False
        self._save_settings(self._make_settings(sensor_calibration=None))
        self._sensor_calibration = None
        self._recent_raw_readings.clear()
        self._reset_after_calibration()
        self._log.info("accelerometer calibration reset")
        return True

    def _make_settings(self, **changes: Any) -> Settings:
        values: dict[str, Any] = {
            "tent_enabled": bool(getattr(self._controller, "tent_enabled", True)),
            "tent_prompt": bool(getattr(self._controller, "tent_prompt", True)),
            "stand_prompt": bool(getattr(self._controller, "stand_prompt", False)),
            "sensor_calibration": self._sensor_calibration,
        }
        values.update(changes)
        return Settings(**values)

    def _reset_after_calibration(self) -> None:
        reset_policy = getattr(
            getattr(self._controller, "policy", None), "reset_after_suspend", None
        )
        if callable(reset_policy):
            reset_policy()
        self._bump_generation()
        self._tick_error_code = None
        self._tick_error_detail = None

    def _save_prompt_settings(
        self,
        *,
        tent_enabled: bool | None = None,
        tent_prompt: bool | None = None,
        stand_prompt: bool | None = None,
    ) -> None:
        """Save preference values, preserving the ones not being changed."""

        changes: dict[str, bool] = {}
        if tent_enabled is not None:
            changes["tent_enabled"] = tent_enabled
        if tent_prompt is not None:
            changes["tent_prompt"] = tent_prompt
        if stand_prompt is not None:
            changes["stand_prompt"] = stand_prompt
        self._save_settings(self._make_settings(**changes))

    def _save_settings(self, settings: Settings) -> None:
        save = getattr(self._settings, "save", None)
        if save is None:
            return
        try:
            save(settings)
        except OSError as error:
            self._log.error("cannot save settings: %s", error)
            raise ServiceError(ErrorCode.CONFIG_WRITE, str(error)) from None

    def confirm_tent(self, epoch: int) -> bool:
        if not self.supported:
            raise ServiceError(ErrorCode.UNSUPPORTED_MODEL)
        try:
            accepted = bool(self._controller.confirm_tent(int(epoch)))
        except (TypeError, ValueError):
            accepted = False
        if not accepted:
            raise ServiceError(ErrorCode.EXPIRED_CONFIRMATION, str(epoch))
        return True

    def confirm_stand(self, epoch: int) -> bool:
        if not self.supported:
            raise ServiceError(ErrorCode.UNSUPPORTED_MODEL)
        try:
            accepted = bool(self._controller.confirm_stand(int(epoch)))
        except (TypeError, ValueError):
            accepted = False
        if not accepted:
            raise ServiceError(ErrorCode.EXPIRED_CONFIRMATION, str(epoch))
        return True

    def session_update(
        self,
        generation: int,
        ready: bool,
        error_code: str | None,
        error_detail: str | None,
        owner: SessionOwner,
    ) -> bool:
        """Accept feedback from the session adapter for the current generation.

        The platform host must authenticate the caller and establish its
        owner identity first. Rejected callers must never refresh a lease.
        """
        if not isinstance(owner, SessionOwner):
            raise TypeError("an authenticated SessionOwner is required")
        now = self._monotonic()
        try:
            generation_value = int(generation)
        except (TypeError, ValueError):
            return False
        if generation_value != int(getattr(self._controller, "generation", 0)):
            self._log.debug(
                "ignoring session update for stale generation %s", generation
            )
            return False
        if (
            self.session_owner is not None
            and owner != self.session_owner
            and now - self.agent_seen < LEASE_SECONDS
        ):
            # Another authenticated user's adapter must wait for the current
            # lease to expire before taking over (fast user switch).
            raise ServiceError(ErrorCode.ACCESS_DENIED, "session-lease-busy")
        self.session_owner = owner
        self.agent_seen = now
        if ready:
            self.agent_ready = generation_value
            self.agent_error_code = None
            self.agent_error_detail = None
        else:
            # ``ready=False`` with an empty code is a plain lease heartbeat /
            # "not applied yet" report from the adapter; it must not become a
            # backend error (the adapter retries display transactions itself).
            self.agent_ready = None
            self._controller.applied = None
            raw_code = str(error_code or "")
            if raw_code:
                code = raw_code if raw_code in ALL_ERROR_CODES else ErrorCode.INTERNAL
                detail: str | None
                if code == ErrorCode.INTERNAL and raw_code != ErrorCode.INTERNAL:
                    detail = error_message(raw_code, error_detail)
                else:
                    detail = error_detail
                self.agent_error_code = code
                self.agent_error_detail = detail
            else:
                self.agent_error_code = None
                self.agent_error_detail = None
        return True

    def sleep_event(self, suspended: bool) -> None:
        """PrepareForSleep: drop the lease, release the relay, invalidate anchors."""
        self.suspended = bool(suspended)
        self._retry_requested = False
        self._log.info("PrepareForSleep: suspended=%s", self.suspended)
        self._wait_for_release()
        try:
            self._actuator.release()
        except Exception:  # noqa: BLE001 - never abort the sleep handler
            self._log.exception("cannot release relay on suspend/resume")
        self._controller.reset_after_suspend()
        self.session_owner = None
        self.agent_ready = None
        self.agent_error_code = None
        self.agent_error_detail = None
        self.agent_seen = float("-inf")
        self._hardware_monitor.reset()
        self._hardware_observation = HardwareObservation(
            None, None, HardwareStatus.ABSENT
        )
        self._prefer_shared = None

    def _bump_generation(self) -> None:
        self._controller.generation += 1
        self._controller.applied = None
        self._controller.error_code = None
        self._controller.error_detail = None
        self.agent_ready = None
        self.agent_error_code = None
        self.agent_error_detail = None

    # -- tick ------------------------------------------------------------

    def tick(self, now: float | None = None) -> None:
        """Run one transaction; contain failures so the host scheduler survives."""
        started = self._monotonic()
        recovering = self._tick_error_code is not None
        self._tick_error_code = None
        self._tick_error_detail = None
        try:
            self._run_tick(self._monotonic() if now is None else now)
        except Exception:  # noqa: BLE001 - top-level protection of the 100 ms loop
            self._log.exception("service tick failed")
            self._tick_error_code = ErrorCode.INTERNAL
            self._tick_error_detail = "tick-failed"
            # Even a transient fault requires a new display acknowledgement.
            if not recovering or self.agent_ready is not None:
                self._bump_generation()
            if self._manage_actuator:
                self._poll_release()
                self._ensure_release(self._controller.generation, Target())
            try:
                self._publish_state()
            except Exception:  # noqa: BLE001 - last resort
                self._log.exception("cannot publish state after tick failure")
        elapsed = self._monotonic() - started
        if elapsed > self._slow_tick_seconds:
            self._log.warning("slow service tick: %.0f ms", elapsed * 1000.0)

    def _run_tick(self, now: float) -> None:
        raw_reading = self._read_sensor()
        # Capture the clock after taking the sensor snapshot.  The acquisition
        # thread may publish a sample between these two operations; using a
        # pre-read timestamp then makes the fresh sample look like it came from
        # the future, which resets the policy run and flashes "unknown".
        boot_now = self._clock()
        self._remember_raw_reading(raw_reading)
        reading = self._apply_sensor_calibration(raw_reading)
        hardware = self._read_hardware(now)

        generation, target = self._controller.update(boot_now, reading, hardware)
        if not self.supported:
            target = Target(mode="unavailable")
        if target != self._target:
            self._log.info(
                "target changed %s -> %s (policy=%s generation=%s sensor=%s)",
                self._target.mode,
                target.mode,
                getattr(self._controller.policy, "mode", "?"),
                generation,
                reading.source,
            )
        self._sync_prefer_shared(hardware, target)
        lease = self._lease_active(now)

        self._poll_release()
        self._drive_actuator(now, lease, generation, target)
        self._pose = self._update_presentation(boot_now, reading, hardware)
        self._target = target
        self._publish_state()

    def _read_sensor(self) -> SensorReading:
        if self.suspended:
            return SensorReading()
        reader = getattr(self._sensor, "reading", None)
        if reader is None:
            return SensorReading()
        reading = reader()
        if not isinstance(reading, SensorReading):
            return SensorReading()
        return reading

    def _remember_raw_reading(self, reading: SensorReading) -> None:
        if not reading.valid or reading.stamp is None:
            return
        if self._recent_raw_readings:
            previous = self._recent_raw_readings[-1]
            if previous.stamp is not None and reading.stamp <= previous.stamp:
                return
        self._recent_raw_readings.append(reading)
        cutoff = reading.stamp - CALIBRATION_WINDOW_SECONDS
        while (
            self._recent_raw_readings
            and self._recent_raw_readings[0].stamp is not None
            and self._recent_raw_readings[0].stamp < cutoff
        ):
            self._recent_raw_readings.popleft()

    def _apply_sensor_calibration(self, reading: SensorReading) -> SensorReading:
        if self._sensor_calibration is None or not reading.valid:
            return reading
        try:
            vector = apply_calibration(
                reading.vector or (0.0, 0.0, 0.0), self._sensor_calibration
            )
        except ValueError:
            self._log.exception("cannot apply sensor calibration")
            return reading
        return SensorReading(vector=vector, stamp=reading.stamp, source=reading.source)

    def _read_hardware(self, now: float) -> bool | None:
        observation = self._hardware_monitor.poll(now)
        self._hardware_observation = observation
        return observation.effective

    def _sync_prefer_shared(self, hardware: bool | None, target: Target) -> None:
        """Hint that the desktop may also need the sensor during tablet use."""
        policy_mode = str(getattr(self._controller.policy, "mode", ""))
        desired = bool(
            hardware is True
            or target.disable_internal_input
            or policy_mode in ("P", "Q", "T", "S")
            # Keep the shared source while rebuilding posture evidence after
            # hardware release. Reopening IIO here interrupts that evidence
            # and can contend with the desktop's still-active sensor buffer.
            or getattr(self._controller.policy, "posture_pending", False)
        )
        if desired == self._prefer_shared:
            return
        self._prefer_shared = desired
        setter = getattr(self._sensor, "set_prefer_shared", None)
        if setter is not None:
            try:
                setter(desired)
            except Exception:  # noqa: BLE001 - sensor preference is best effort
                self._log.exception("cannot update sensor source preference")

    def _lease_active(self, now: float) -> bool:
        if self.suspended or self.session_owner is None:
            return False
        return now - self.agent_seen < LEASE_SECONDS

    def _prepared(self, lease: bool, generation: int) -> bool:
        return (
            lease
            and self.agent_ready == generation
            and self.agent_error_code is None
        )

    def _drive_actuator(
        self, now: float, lease: bool, generation: int, target: Target
    ) -> None:
        if not self._manage_actuator:
            return
        if self._release_task is not None:
            # The worker serializes all actuator operations.  A new keep must
            # wait until the pending release/recovery has completed.
            return
        prepared = self._prepared(lease, generation)
        needs_input_control = target.disable_internal_input
        hardware_off = self._hardware_observation.effective is False
        if needs_input_control and self._controller.error_code == ErrorCode.NATIVE_RECOVERY:
            # A fast display acknowledgement cannot make a failed reset safe.
            self._ensure_release(generation, target)
            return
        try:
            if self._follow_hardware(now, lease, generation, target, prepared):
                return
            if needs_input_control and hardware_off and prepared:
                self._actuator.keep(now)
                self._following_hardware = False
                self._handoff_until = None
                self._last_release_key = None
                state = self._actuator.state
                # ``keep`` may spawn the relay and can block for a moment: only
                # acknowledge success if the session lease is still valid
                # against the fresh clock (review Q-03).
                lease = self._lease_active(self._monotonic())
                prepared = self._prepared(lease, generation)
                if state is not None and state.error is None and state.satisfies(target):
                    if prepared:
                        self._controller.acknowledge(generation, True)
                elif state is not None and state.error:
                    self._controller.acknowledge(
                        generation, False, ErrorCode.RELAY_INPUT, state.error
                    )
            elif (
                needs_input_control
                and hardware_off
                and lease
                and self.agent_error_code is None
                and self._actuator.running
                and self._actuator.state is not None
                and not self._following_hardware
            ):
                # Keep software ownership while switching between two software
                # tablet orientations; a failed preparation still revokes it.
                self._actuator.keep(now)
                self._last_release_key = None
            else:
                key = (generation, target)
                self._ensure_release(generation, target)
                # Release completion and the display acknowledgement can
                # arrive in either order. Only targets that need no input
                # grab can be applied on release.
                if (
                    self._release_task is None
                    and self._last_release_key == key
                    and self._actuator.error_code is None
                    and target.mode in ("laptop", "hardware")
                    and not target.disable_internal_input
                    and self._controller.applied != target
                    and self._prepared(self._lease_active(self._monotonic()), generation)
                ):
                    self._controller.acknowledge(generation, True)
        except ActuatorError as error:
            self._controller.acknowledge(generation, False, error.code, error.detail)
            # A failed keep may have changed the relay even after this target
            # was released before. Record the failure before scheduling cleanup
            # so a later recovery failure can supersede the original error.
            self._last_release_key = None
            self._ensure_release(generation, target)

    def _follow_hardware(
        self, now: float, lease: bool, generation: int, target: Target, prepared: bool
    ) -> bool:
        observation = self._hardware_observation
        healthy = (
            lease and self.supported and self.agent_error_code is None
            and self._controller.error_code is None
            and self._sensor.info.error is None
            and bool(getattr(self._controller.policy, "fresh", False))
            and observation.status == HardwareStatus.OK and observation.raw is not None
        )
        if not healthy:
            if self._following_hardware:
                self._ensure_release(generation, target)
                return True
            return False
        if observation.raw is True and target.mode == "hardware":
            # Retain an existing software bridge. Starting one after native
            # tablet ON could grab a switch already paired by the desktop and
            # hide its OFF from that desktop's input-device suspension state.
            if not (self._following_hardware or self._actuator.running):
                return False
            self._following_hardware = True
            self._handoff_until = None
        elif self._following_hardware and observation.raw is False:
            if self._handoff_until is None:
                self._handoff_until = now + HARDWARE_HANDOFF_SECONDS
            waiting = (
                target.mode == "unavailable"
                and bool(getattr(self._controller.policy, "posture_pending", False))
            )
            if now >= self._handoff_until or not (
                waiting or (target.disable_internal_input and not prepared)
            ):
                return False
        else:
            return False
        self._actuator.follow_hardware(now)
        self._last_release_key = None
        state = self._actuator.state
        if state is not None and state.error:
            raise ActuatorError(ErrorCode.RELAY_INPUT, state.error)
        if (target.mode == "hardware" and state is not None and state.satisfies(target)
                and self._prepared(self._lease_active(self._monotonic()), generation)):
            self._controller.acknowledge(generation, True)
        return True

    def _ensure_release(self, generation: int, target: Target) -> None:
        self._following_hardware = False
        self._handoff_until = None
        key = (generation, target)
        if self._release_task is None and self._last_release_key != key:
            self._last_release_key = key
            self._submit_release(generation)

    def _queue_retry(self) -> None:
        if self._release_task is not None and self._release_task.retry:
            return  # the running reset also serves the newer generation
        self._retry_requested = True
        self._start_retry()

    def _start_retry(self) -> None:
        if not self._retry_requested or self._release_task is not None:
            return
        self._retry_requested = False
        self._last_release_key = None
        self._submit_release(self._controller.generation, retry=True)

    def _submit_release(self, generation: int, *, retry: bool = False) -> None:
        self._following_hardware = False
        self._handoff_until = None
        try:
            future = self._executor.submit(
                self._actuator.retry if retry else self._actuator.release
            )
        except Exception as error:  # noqa: BLE001 - executor shutdown race
            future = Future()
            future.set_exception(error)
        self._release_task = _ReleaseTask(future, generation, retry)
        # InlineExecutor (tests) completes synchronously.  Poll once so the
        # production thread pool keeps the same one-tick acknowledgement flow;
        # a real release remains asynchronous and is consumed on a later tick.
        self._poll_release()

    def _wait_for_release(self) -> None:
        """Join an in-flight worker release before a synchronous lifecycle call."""
        self._following_hardware = False
        self._handoff_until = None

        task = self._release_task
        if task is None:
            return
        self._release_task = None
        self._last_release_key = None
        try:
            task.future.result()
        except Exception:  # noqa: BLE001 - the caller performs its own recovery
            self._log.exception("background relay release failed")

    def _poll_release(self) -> None:
        task = self._release_task
        if task is None or not task.future.done():
            return
        self._release_task = None
        # A reset serves every target queued while it runs. Its failure is a
        # current backend fault; ordinary release results stay generation-bound.
        generation = self._controller.generation if task.retry else task.generation
        try:
            task.future.result()
        except Exception as error:  # noqa: BLE001 - actuator errors are state input
            self._last_release_key = None
            code = getattr(
                error, "code", ErrorCode.NATIVE_RECOVERY if task.retry else ErrorCode.INTERNAL
            )
            detail = getattr(error, "detail", None) or str(error)
            self._controller.acknowledge(generation, False, code, detail)
            self._start_retry()
            return

        if self._actuator.error_code:
            self._controller.acknowledge(
                generation,
                False,
                self._actuator.error_code,
                self._actuator.error_detail,
            )
        self._start_retry()

    def _update_presentation(
        self, boot_now: float, reading: SensorReading, hardware: bool | None
    ) -> Pose:
        candidate = Pose.UNKNOWN
        fresh = (
            reading.valid
            and reading.stamp is not None
            and 0 <= boot_now - reading.stamp <= self._stale_seconds
        )
        if hardware is True:
            candidate = Pose.TABLET
        elif hardware is False and fresh and getattr(self._controller.policy, "stable", False):
            mode = getattr(self._controller.policy, "mode", "D")
            candidate = MODE_TO_POSE.get(str(mode), Pose.UNKNOWN)
        try:
            return Pose(
                self._presentation.update(
                    boot_now,
                    candidate,
                    hardware,
                    reading.stamp if fresh else None,
                )
            )
        except Exception:  # noqa: BLE001 - presentation is cosmetic
            self._log.exception("presentation update failed")
            return self._pose

    # -- state -----------------------------------------------------------

    def _effective_error(self) -> tuple[str | None, str | None]:
        code = getattr(self._controller, "error_code", None)
        detail = getattr(self._controller, "error_detail", None)
        if code is None and self.agent_error_code is not None and self.agent_ready is None:
            code, detail = self.agent_error_code, self.agent_error_detail
        resetting = self._retry_requested or (
            self._release_task is not None and self._release_task.retry
        )
        if code is None and self._manage_actuator and not resetting:
            # The old error is not the result of the reset still in progress.
            # Completion publishes a new failure before any input takeover.
            actuator_code = getattr(self._actuator, "error_code", None)
            if actuator_code is not None:
                code = actuator_code
                detail = getattr(self._actuator, "error_detail", None)
        if code is None:
            code, detail = self._tick_error_code, self._tick_error_detail
        return code, detail

    def _build_state(self, lease: bool) -> StateSnapshot:
        sensor_info = self._sensor.info
        """Build the public snapshot from the current transaction state."""
        error_code, error_detail = self._effective_error()
        target = self._target if self.supported else Target(mode="unavailable")
        backend = (
            BackendState.UNSUPPORTED
            if not self.supported
            else BackendState.DEVELOPMENT
            if self.development
            else BackendState.ERROR
            if error_code
            else BackendState.READY
        )
        manual_value = str(getattr(self._controller, "manual", "auto"))
        manual = (
            Mode(manual_value)
            if manual_value in tuple(mode.value for mode in Mode)
            else Mode.AUTO
        )
        applied = getattr(self._controller, "applied", None)
        if not isinstance(applied, Target):
            applied = None
        return StateSnapshot(
            instance=self._instance,
            generation=int(getattr(self._controller, "generation", 0)),
            pose=self._pose,
            hardware=self._hardware_observation.effective,
            hardware_status=self._hardware_observation.status,
            manual=manual,
            tent_enabled=bool(getattr(self._controller, "tent_enabled", True)),
            tent_prompt=bool(getattr(self._controller, "tent_prompt", True)),
            stand_prompt=bool(getattr(self._controller, "stand_prompt", False)),
            target=target,
            applied=applied,
            pending_notification=(
                None
                if not self.supported
                else getattr(self._controller, "pending_notification", None)
            ),
            pending_prompt=(
                None
                if not self.supported
                else getattr(self._controller, "pending_prompt", None)
            ),
            backend=backend,
            error_code=error_code,
            error_detail=error_detail,
            session_connected=lease,
            supported=self.supported,
            support_tier=self.support_tier,
            sensor_source=sensor_info.backend or "starting",
            sensor_info=sensor_info,
            sensor_error=sensor_info.error,
            posture_pending=bool(
                self.supported and lease and not error_code
                and sensor_info.ready and not sensor_info.error
                and target.mode == "unavailable"
                and getattr(self._controller.policy, "posture_pending", False)
            ),
            sensor_calibrated=self._sensor_calibration is not None,
            profile=self._device.id,
            model=self._device.product,
            vendor=self._device.vendor,
        )

    def _publish_state(self) -> None:
        # Relay work can outlast the lease. Use the same fresh lease check
        # for clearing the applied target and reporting connectivity.
        lease = self._lease_active(self._monotonic())
        if not self._prepared(lease, self._controller.generation):
            self._controller.applied = None
        snapshot = self._build_state(lease)
        self._state = snapshot
        text = dumps_state(snapshot)
        if text == self._state_json:
            return
        self._state_json = text
        if self._on_state is not None:
            try:
                self._on_state(snapshot)
            except Exception:  # noqa: BLE001 - a failed listener must not stop the loop
                self._log.exception("state listener failed")


__all__ = ["MODE_TO_POSE", "Engine", "ServiceError", "guard"]


def __getattr__(name: str):
    if name == "guard":
        from .callbacks import guard
        return guard
    raise AttributeError(name)
