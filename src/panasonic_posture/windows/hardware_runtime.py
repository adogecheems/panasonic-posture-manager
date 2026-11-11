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

The CSM is an output only.  Before the first valid ETW event the physical
state is unknown, so the host accepts only not-ready session heartbeats and
no display or CSM transition may be confirmed.  The desktop agent still
starts before that event so journal recovery and display setup do not race
the first physical posture change.  A later ETW failure revokes admission
again; the host retries the source instead of reading the indicator it may
have written.
"""
from __future__ import annotations

from collections.abc import Callable
import time
from typing import TYPE_CHECKING, Protocol

from ..model import DeviceInfo, HardwareStatus
from ..service.engine import Engine

if TYPE_CHECKING:
    from .hardware_server import HardwareServer
    from .session_launch import NativeSessionLauncher
    from .trusted_runtime import TrustedRuntime


class ManagedSensor(Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...


class PhysicalSwitch(Protocol):
    def start(self) -> None: ...
    def query(self) -> tuple[bool | None, HardwareStatus]: ...
    def close(self) -> None: ...


class SessionLifecycle(Protocol):
    closed: bool
    def poll(self) -> None: ...
    def request_stop(self) -> None: ...
    def close(self) -> None: ...


class ServerLifecycle(Protocol):
    def register_session_process(self, peer: object) -> None: ...
    def poll(self) -> None: ...
    def close(self) -> None: ...


class LauncherLifecycle(Protocol):
    def close(self) -> None: ...


class RuntimeLifecycle(Protocol):
    def close(self) -> None: ...


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
        supervisor_factory: Callable[..., SessionLifecycle] | None = None,
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
            from .session_supervisor import SessionSupervisor
            supervisor_factory = SessionSupervisor
        self.supervisor_factory = supervisor_factory
        self.clock = clock
        self.sleep = sleep
        self.stop_timeout = float(stop_timeout)
        self.server = None
        self.launcher = None
        self.supervisor = None
        self._bootstrap_until: float | None = None
        self.started = self.closed = False

    def admitted(self) -> bool:
        """Return whether a trusted session may currently own a display lease."""
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
            self.engine.start()
            if getattr(self.switch, 'bootstrap', None) is not None:
                self._bootstrap_until = self.clock() + 5.
            self.server = self.server_factory(self.admitted)
            self.launcher = self.launcher_factory()
            self.supervisor = self.supervisor_factory(
                self.launcher,
                self.server.register_session_process,
                clock=self.clock,
            )
        except BaseException:
            self.close()
            raise

    def poll(self) -> None:
        if not self.started or self.closed:
            return
        if self._bootstrap_until is not None:
            self.switch.bootstrap()
            if self.admitted():
                self._bootstrap_until = None
            elif self.clock() >= self._bootstrap_until:
                finalize = getattr(self.switch, 'finalize_bootstrap', None)
                if finalize is not None:
                    finalize(False)
                self._bootstrap_until = None
        # The supervisor registers before resume and the server owns the only
        # engine tick.  Do not call engine.tick() here: HardwareServer.poll()
        # already performs host.tick() followed by exactly one engine tick.
        self.supervisor.poll()
        self.server.poll()

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
            if supervisor.closed:
                try:
                    supervisor.close()
                except BaseException as error:
                    errors.append(error)
            else:
                # close() is intentionally a finalizer, not a second stop
                # request.  Keep the incomplete supervisor visible to callers.
                try:
                    supervisor.close()
                except BaseException as error:
                    errors.append(error)

        for resource in (self.launcher, self.server, self.sensor, self.switch, self.runtime):
            if resource is None:
                continue
            try:
                resource.close()
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

    @classmethod
    def installed(
        cls,
        *,
        runtime: TrustedRuntime | None = None,
        settings=None,
        device: DeviceInfo | None = None,
        server_factory: Callable[[Callable[[], bool]], HardwareServer] | None = None,
        launcher_factory: Callable[[], NativeSessionLauncher] | None = None,
        supervisor_factory: Callable[..., SessionLifecycle] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        stop_timeout: float = 12.,
    ) -> 'HardwareRuntime':
        """Build the production composition from the pinned installation."""
        from .hardware_application import create_hardware_runtime

        return create_hardware_runtime(
            runtime=runtime,
            settings=settings,
            device=device,
            server_factory=server_factory,
            launcher_factory=launcher_factory,
            supervisor_factory=supervisor_factory,
            clock=clock,
            sleep=sleep,
            stop_timeout=stop_timeout,
        )


__all__ = [
    'HardwareRuntime', 'LauncherLifecycle', 'ManagedSensor', 'PhysicalSwitch',
    'RuntimeLifecycle', 'ServerLifecycle', 'SessionLifecycle',
]
