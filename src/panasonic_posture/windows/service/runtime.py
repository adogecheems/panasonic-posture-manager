"""Explicit composition root for the privileged Windows hardware host.

The installed service constructs this object after it has acquired LocalSystem
authority.  This module deliberately does not install a service, create a
scheduled task, load a driver, or open an input device.  Tests inject the
host, sensor, launcher, and runtime ports so the lifecycle can run without
touching a real console or display.

Startup order is part of the safety contract:

1. start the physical ETW consumer;
2. start the WinRT sensor worker;
3. only then create the RPC endpoint and the trusted-session supervisor.

The CSM is an output only. Generic hardware requires the first valid ETW
event before display admission; explicit profiles may use their one-time
read-only startup baseline while ETW is healthy. Until admission opens, the
host accepts only not-ready session heartbeats. The desktop agent may start
before admission, but cannot confirm a display or CSM transition. A later
ETW failure revokes admission without re-reading a software-owned indicator.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
import logging
import threading
import time
from typing import TYPE_CHECKING, Protocol

from panasonic_posture.model import DeviceInfo, HardwareStatus
from panasonic_posture.service.engine import Engine
from panasonic_posture.windows.common.power import SuspendResumeSource

if TYPE_CHECKING:
    from panasonic_posture.windows.common.identity import ConsoleIdentity
    from panasonic_posture.windows.service.server import HardwareServer
    from panasonic_posture.windows.service.launch import NativeSessionLauncher
    from panasonic_posture.windows.service.trusted_runtime import TrustedRuntime

log = logging.getLogger(__name__)

RESUME_GAP_SECONDS = 1.5


def _state_signature(state) -> tuple:
    """Cheap stable signature of the fields the UI and tray observe.

    The wake event is published only when this signature changes, so a
    spurious publish (or a missed one) costs at most one extra validated
    ``get_state`` on the observer side.
    """
    return (
        state.generation, state.pose, state.hardware, state.hardware_status,
        state.manual, state.target, state.applied, state.pending_notification,
        state.pending_prompt, state.posture_pending, state.backend,
        state.error_code, state.session_connected, state.touch_wake,
    )


class ManagedSensor(Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...


class PhysicalSwitch(Protocol):
    def start(self) -> None: ...
    def query(self) -> tuple[bool | None, HardwareStatus]: ...
    def bootstrap(self) -> None: ...
    def finalize_bootstrap(self, fallback: bool) -> None: ...
    def invalidate(self) -> None: ...
    def recover(self) -> None: ...
    def close(self) -> None: ...


class SessionLifecycle(Protocol):
    closed: bool
    def poll(self) -> None: ...
    def request_stop(self) -> None: ...
    def close(self) -> None: ...
    def defer_close(self, after_close: Callable[[], None]) -> Future: ...


class ServerLifecycle(Protocol):
    def register_session_process(self, peer: ConsoleIdentity | None) -> None: ...
    def poll(self) -> None: ...
    def close(self) -> None: ...


class LauncherLifecycle(Protocol):
    def close(self) -> None: ...


class RuntimeLifecycle(Protocol):
    def close(self) -> None: ...


class WakeLifecycle(Protocol):
    def create(self) -> None: ...
    def publish(self) -> None: ...
    def close(self) -> None: ...


class SupervisorFactory(Protocol):
    def __call__(
        self,
        launcher: LauncherLifecycle,
        register: Callable[[ConsoleIdentity | None], None],
        *,
        clock: Callable[[], float],
    ) -> SessionLifecycle: ...


class HardwareRuntime:
    """Own one hardware engine and its trusted session lifecycle."""

    def __init__(
        self,
        *,
        engine: Engine,
        switch: PhysicalSwitch,
        sensor: ManagedSensor,
        runtime: RuntimeLifecycle,
        server_factory: Callable[[Callable[[], bool]], ServerLifecycle],
        launcher_factory: Callable[[], LauncherLifecycle],
        supervisor_factory: SupervisorFactory | None = None,
        wake: WakeLifecycle | None = None,
        power_factory: Callable[[Callable[[bool], None]], SuspendResumeSource] = SuspendResumeSource,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_timeout: float = 12.,
    ) -> None:
        if clock is None or sleep is None:
            raise TypeError('runtime clock and sleep ports are required')
        if not isinstance(stop_timeout, (int, float)) or isinstance(stop_timeout, bool) or stop_timeout <= 0:
            raise ValueError('runtime stop timeout must be positive')
        self.engine = engine
        self.switch = switch
        self.sensor = sensor
        self.runtime = runtime
        self.server_factory = server_factory
        self.launcher_factory = launcher_factory
        if supervisor_factory is None:
            from panasonic_posture.windows.service.supervisor import SessionSupervisor
            supervisor_factory = SessionSupervisor
        self.supervisor_factory = supervisor_factory
        self.clock = clock
        self.sleep = sleep
        self.stop_timeout = float(stop_timeout)
        self.server = None
        self.launcher = None
        self.supervisor = None
        self.wake = wake
        self._power_factory = power_factory
        self._power = None
        self._power_lock = threading.Lock()
        self._suspended = self._resume_seen = False
        self._power_epoch = self._power_epoch_seen = 0
        self._bootstrap_until: float | None = None
        self._last_poll_at: float | None = None
        self._wake_signature: tuple | None = None
        self._cleanup_task: Future | None = None
        self.started = self.closed = False

    def admitted(self) -> bool:
        """Return whether a trusted session may currently own a display lease."""
        with self._power_lock:
            if (self._suspended or self._power_epoch != self._power_epoch_seen
                    or getattr(self._power, 'error', None) is not None):
                return False
        try:
            value, status = self.switch.query()
        except (OSError, ValueError, RuntimeError):
            return False
        return status == HardwareStatus.OK and type(value) is bool

    def start(self) -> None:
        if self.started or self.closed:
            raise RuntimeError('hardware runtime already started or closed')
        self.started = True
        try:
            # ETW is the only physical input.  It must be live before the
            # sensor, RPC endpoint or any session display writer exists.
            self.switch.start()
            self._power = self._power_factory(self._power_event)
            self.engine.start()
            self._bootstrap_until = self.clock() + 5.
            self.server = self.server_factory(self.admitted)
            self.launcher = self.launcher_factory()
            self.supervisor = self.supervisor_factory(
                self.launcher,
                self.server.register_session_process,
                clock=self.clock,
            )
            if self.wake is not None:
                try:
                    self.wake.create()
                except (OSError, ValueError):
                    # A missing wake event only degrades UI latency to the
                    # polling fallback; the authenticated RPC path is intact.
                    log.warning(
                        'hardware wake event unavailable; UI polling refresh remains',
                        exc_info=True,
                    )
                    self.wake = None
            self._last_poll_at = self.clock()
        except BaseException:
            self.close()
            raise

    def _power_event(self, suspended: bool) -> None:
        # Native callbacks may only invalidate the guarded hardware cache and
        # signal owner-loop work. They never touch the engine or a display.
        with self._power_lock:
            if self.closed or (suspended and self._suspended) or (not suspended and self._resume_seen):
                return
            # Also clear on the first resume: an event delivered after the
            # suspend callback but before the machine actually slept is old.
            self.switch.invalidate()
            self._suspended = suspended
            self._resume_seen = not suspended
            self._power_epoch += 1

    def poll(self) -> None:
        if not self.started or self.closed:
            return
        now = self.clock()
        with self._power_lock:
            power_changed = self._power_epoch != self._power_epoch_seen
            self._power_epoch_seen = self._power_epoch
        gap = (self._last_poll_at is not None
               and now - self._last_poll_at >= RESUME_GAP_SECONDS)
        if power_changed or gap:
            # Windows monotonic time advances across sleep.  The service owner
            # loop resumes before it reads more RPC work, so discard the old
            # pose before a desktop session can replay a pre-sleep tent target.
            log.info("session power boundary; requiring a fresh posture")
            self.engine.reset_after_session_context_loss()
            if not power_changed:
                # Lost native callbacks still cannot authorize a sleep-era
                # cache. With callbacks, preserve an ETW event already received
                # after resume and before this first owner-loop iteration.
                self.switch.invalidate()
            self._bootstrap_until = None
        self._last_poll_at = now
        if self._bootstrap_until is not None:
            self.switch.bootstrap()
            if self.admitted():
                self._bootstrap_until = None
            elif self.clock() >= self._bootstrap_until:
                self.switch.finalize_bootstrap(False)
                self._bootstrap_until = None
        with self._power_lock:
            recovering = not self._suspended and self._power_epoch == self._power_epoch_seen
        if recovering:
            self.switch.recover()
        # The supervisor registers before resume and the server owns the only
        # engine tick.  Do not call engine.tick() here: HardwareServer.poll()
        # already performs host.tick() followed by exactly one engine tick.
        self.supervisor.poll()
        self.server.poll()
        if self.wake is not None:
            self._publish_wake()

    def _publish_wake(self) -> None:
        signature = _state_signature(self.engine.state)
        if signature == self._wake_signature:
            return
        self._wake_signature = signature
        try:
            self.wake.publish()
        except (OSError, ValueError):
            # A lost publish only falls back to the observer polling refresh.
            log.warning('hardware wake publish failed', exc_info=True)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[BaseException] = []
        supervisor = self.supervisor
        if supervisor is not None:
            try:
                supervisor.request_stop()
            except BaseException as error:
                errors.append(error)
            deadline = self.clock() + self.stop_timeout
            attempts = max(1, int(self.stop_timeout / .01))
            while not supervisor.closed and attempts:
                attempts -= 1
                try:
                    supervisor.poll()
                except BaseException as error:
                    errors.append(error)
                if self.server is not None:
                    try:
                        self.server.poll()
                    except BaseException as error:
                        errors.append(error)
                if not supervisor.closed:
                    self.sleep(.01)
                if self.clock() >= deadline:
                    break
            # close() is intentionally a finalizer, not a second stop request.
            # It may retain incomplete child resources for a later retry.
            try:
                supervisor.close()
            except BaseException as error:
                errors.append(error)

        if supervisor is not None and not supervisor.closed:
            # Never reacquire the launcher's lock on the owner loop after a
            # timed-out WTS/create operation. Its worker retains the runtime
            # pins and reaps late children before releasing the installation.
            resources = (self._power, self.server, self.sensor, self.switch)
            try:
                self._cleanup_task = supervisor.defer_close(self.runtime.close)
                self._cleanup_task.add_done_callback(self._deferred_close_done)
            except BaseException as error:
                # If scheduling itself fails, retain pins rather than racing
                # native creation; still close the hardware and RPC resources.
                errors.append(error)
        else:
            resources = (self._power, self.launcher, self.server, self.sensor, self.switch, self.runtime)
        for resource in resources:
            if resource is None:
                continue
            try:
                resource.close()
            except BaseException as error:
                errors.append(error)
        if self.wake is not None:
            try:
                self.wake.close()
            except BaseException as error:
                errors.append(error)
        # HardwareServer.close() normally owns this call.  Keep the engine
        # shutdown explicit for a partially constructed runtime and for
        # injected servers that do not own the engine.
        try:
            self.engine.shutdown()
        except BaseException as error:
            errors.append(error)
        if errors:
            raise OSError('hardware runtime cleanup is incomplete') from errors[0]

    @staticmethod
    def _deferred_close_done(task: Future) -> None:
        try:
            task.result()
        except Exception:
            log.exception('deferred session cleanup failed; installation pins retained')

    @classmethod
    def installed(
        cls,
        *,
        runtime: TrustedRuntime | None = None,
        settings=None,
        device: DeviceInfo | None = None,
        server_factory: Callable[[Callable[[], bool]], HardwareServer] | None = None,
        launcher_factory: Callable[[], NativeSessionLauncher] | None = None,
        supervisor_factory: SupervisorFactory | None = None,
        wake: WakeLifecycle | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_timeout: float = 12.,
    ) -> 'HardwareRuntime':
        """Build the production composition from the pinned installation."""
        from panasonic_posture.windows.service.application import create_hardware_runtime

        return create_hardware_runtime(
            runtime=runtime,
            settings=settings,
            device=device,
            server_factory=server_factory,
            launcher_factory=launcher_factory,
            supervisor_factory=supervisor_factory,
            wake=wake,
            clock=clock,
            sleep=sleep,
            stop_timeout=stop_timeout,
        )


__all__ = [
    'HardwareRuntime', 'LauncherLifecycle', 'ManagedSensor', 'PhysicalSwitch',
    'RESUME_GAP_SECONDS', 'RuntimeLifecycle', 'ServerLifecycle', 'SessionLifecycle',
    'SupervisorFactory', 'WakeLifecycle', '_state_signature',
]
