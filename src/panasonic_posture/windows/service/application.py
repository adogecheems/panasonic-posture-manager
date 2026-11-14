"""Production composition root for the installed Windows hardware service."""
from collections.abc import Callable
import time

from panasonic_posture.controller import Controller
from panasonic_posture.model import DeviceInfo
from panasonic_posture.presentation import PosePresentation
from panasonic_posture.service.engine import Engine
from panasonic_posture.windows.hardware.display_only_input import DisplayOnlyInput
from panasonic_posture.windows.hardware.etw_switch import AcpiPhysicalSwitch
from panasonic_posture.windows.service.runtime import (
    HardwareRuntime,
    SupervisorFactory,
    WakeLifecycle,
)
from panasonic_posture.windows.service.server import HardwareServer
from panasonic_posture.windows.hardware.machine_profile import (
    WindowsProfile,
    resolve_windows_profile,
)
from panasonic_posture.windows.hardware.sensor import WindowsSensor
from panasonic_posture.windows.service.launch import NativeSessionLauncher
from panasonic_posture.windows.hardware.slate_bootstrap import (
    StartupPhysicalBaseline,
    read_bootstrap_slate_state,
)
from panasonic_posture.windows.service.trusted_runtime import TrustedRuntime
from panasonic_posture.windows.ipc.wake import WakeEvent


def create_hardware_runtime(
    *,
    runtime: TrustedRuntime | None = None,
    settings=None,
    device: DeviceInfo | None = None,
    profile: WindowsProfile | None = None,
    server_factory: Callable[[Callable[[], bool]], HardwareServer] | None = None,
    launcher_factory: Callable[[], NativeSessionLauncher] | None = None,
    supervisor_factory: SupervisorFactory | None = None,
    wake: WakeLifecycle | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_timeout: float = 12.,
) -> HardwareRuntime:
    """Build the production graph without starting any hardware resource."""
    runtime = runtime if runtime is not None else TrustedRuntime.current()
    resolved_profile = profile if profile is not None else resolve_windows_profile()
    sensor = WindowsSensor(
        mount_matrix=resolved_profile.mount_matrix,
        include_orientation=False,
    )
    baseline = StartupPhysicalBaseline(
        slate_reader=read_bootstrap_slate_state,
        sensor=sensor,
        clock=clock,
    )
    switch = AcpiPhysicalSwitch(
        initial_reader=baseline,
        trusted_startup_baseline=resolved_profile.support_tier == 'validated',
    )
    engine = Engine(
        controller=Controller(input_control=False),
        sensor=sensor,
        actuator=DisplayOnlyInput(),
        presentation=PosePresentation(),
        settings=settings,
        device=device or DeviceInfo(
            id=resolved_profile.id,
            vendor=resolved_profile.vendor,
            product=resolved_profile.product,
            platform='windows',
            support_tier=resolved_profile.support_tier,
        ),
        hardware_query=switch.query,
        monotonic=clock,
    )

    def make_server(admission: Callable[[], bool]) -> HardwareServer:
        return HardwareServer(engine, session_admission=admission)

    def make_launcher() -> NativeSessionLauncher:
        return NativeSessionLauncher(runtime)

    wake_event = wake if wake is not None else WakeEvent()

    return HardwareRuntime(
        engine=engine,
        switch=switch,
        sensor=sensor,
        runtime=runtime,
        server_factory=server_factory or make_server,
        launcher_factory=launcher_factory or make_launcher,
        supervisor_factory=supervisor_factory,
        wake=wake_event,
        clock=clock,
        sleep=sleep,
        stop_timeout=stop_timeout,
    )


__all__ = ['create_hardware_runtime']
