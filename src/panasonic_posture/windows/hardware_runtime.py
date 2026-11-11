"""Explicit composition root for the privileged Windows hardware host.

The installed service constructs this object after it has acquired LocalSystem
authority.  This module deliberately does not install a service, create a
scheduled task, load a driver, or open an input device.  Tests inject the
host, sensor, launcher, and runtime ports so the lifecycle can run without
touching a real console or display.

Startup order is part of the safety contract:

1. start the physical ETW consumer and establish its clean CSM baseline;
2. start the WinRT sensor worker;
3. only then create the RPC endpoint and the trusted-session supervisor.

If the physical source is absent or read-only, the host still starts in a
read-only state.  It must not accept display receipts or launch a trusted
session until a valid boolean physical observation is available again.
"""
from __future__ import annotations

from collections.abc import Callable
import time
from typing import Protocol

from ..controller import Controller
from ..model import DeviceInfo, HardwareStatus
from ..presentation import PosePresentation
from ..service.engine import Engine
from .display_only_input import DisplayOnlyInput
from .etw_switch import AcpiPhysicalSwitch
from .hardware_ipc import HardwareServer
from .screen_session import ProfileSensor, verified_machine
from .sensor import IDENTITY, WindowsSensor
from .session_launch import NativeSessionLauncher
from .session_supervisor import SessionSupervisor
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


class HardwareRuntime:
    """Own one hardware engine and its trusted session lifecycle."""

    def __init__(
        self,
        *,
        engine: Engine,
        switch: PhysicalSwitch,
        sensor: ManagedSensor,
        runtime: TrustedRuntime,
        server_factory: Callable[[Callable[[], bool]], HardwareServer],
        launcher_factory: Callable[[], NativeSessionLauncher],
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
        self.supervisor_factory = supervisor_factory or SessionSupervisor
        self.clock = clock
        self.sleep = sleep
        self.stop_timeout = float(stop_timeout)
        self.server = None
        self.launcher = None
        self.supervisor = None
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
            # The ETW listener must be live before the CSM baseline is read.
            # AcpiPhysicalSwitch.start() enforces that ordering internally.
            self.switch.start()
            self.engine.start()
            self.server = self.server_factory(self.admitted)
            self.launcher = self.launcher_factory()
            self.supervisor = self.supervisor_factory(
                self.launcher,
                self.server.register_session_process,
                clock=self.clock,
                admit=self.admitted,
            )
        except BaseException:
            self.close()
            raise

    def poll(self) -> None:
        if not self.started or self.closed:
            return
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
        runtime = runtime or TrustedRuntime.current()
        vendor, product = verified_machine()
        switch = AcpiPhysicalSwitch()
        sensor = ProfileSensor(WindowsSensor(
            mount_matrix=IDENTITY,
            include_orientation=False,
        ))
        engine = Engine(
            controller=Controller(input_control=False),
            sensor=sensor,
            actuator=DisplayOnlyInput(),
            presentation=PosePresentation(),
            settings=settings,
            device=device or DeviceInfo(
                id='windows-cfqv8-hardware',
                vendor=vendor,
                product=product,
                platform='windows',
                support_tier='compatible',
            ),
            hardware_query=switch.query,
            monotonic=clock,
        )

        def make_server(admission: Callable[[], bool]) -> HardwareServer:
            return HardwareServer(engine, session_admission=admission)

        def make_launcher() -> NativeSessionLauncher:
            return NativeSessionLauncher(runtime)

        return cls(
            engine=engine,
            switch=switch,
            sensor=sensor,
            runtime=runtime,
            server_factory=server_factory or make_server,
            launcher_factory=launcher_factory or make_launcher,
            supervisor_factory=supervisor_factory,
            clock=clock,
            sleep=sleep,
            stop_timeout=stop_timeout,
        )


__all__ = ['HardwareRuntime', 'ManagedSensor', 'PhysicalSwitch', 'SessionLifecycle']
