"""Production composition root for the installed Windows hardware service."""
from collections.abc import Callable
import time

from ..controller import Controller
from ..model import DeviceInfo
from ..presentation import PosePresentation
from ..service.engine import Engine
from .display_only_input import DisplayOnlyInput
from .etw_switch import AcpiPhysicalSwitch
from .hardware_runtime import HardwareRuntime, SessionLifecycle
from .hardware_server import HardwareServer
from .machine_profile import ProfileSensor, verified_machine
from .sensor import IDENTITY, WindowsSensor
from .session_launch import NativeSessionLauncher
from .trusted_runtime import TrustedRuntime


def create_hardware_runtime(
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
) -> HardwareRuntime:
    """Build the production graph without starting any hardware resource."""
    runtime = runtime if runtime is not None else TrustedRuntime.current()
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

    return HardwareRuntime(
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


__all__ = ['create_hardware_runtime']
