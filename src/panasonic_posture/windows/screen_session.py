"""Ordinary-user screen control using the shared engine and session runtime.

The engine stays on the backend owner thread. Display workers exchange bounded
requests with it; they never mutate the engine or manufacture input receipts.
This route never constructs privileged hardware ports or opens their pipe.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
import time
from typing import Protocol

from ..contracts import ErrorCode, MODES
from ..controller import Controller
from ..model import DeviceInfo, HardwareStatus
from ..presentation import PosePresentation
from ..service.engine import Engine
from ..service.ports import SensorBackend, SessionOwner, SettingsBackend
from ..session.failures import DisplayError
from ..session.runtime import SessionRuntime
from ..session.ports import DisplayBackend, DisplayRefresh
from .display_only_input import DisplayOnlyInput
from .hardware_release import HardwareReleaseCoordinator
from .machine_profile import ProfileSensor, verified_machine
from .native_preflight import require_native_user
from .operations import dispatch_engine_operation
from .session_bridge import SessionBridge
from .session_display import (
    QualifiedDisplay, QualifiedRefresh, SessionContext, SlatePosture,
)

SCREEN_MODES = MODES


class ManagedSensor(SensorBackend, Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...


class SlateSwitch(Protocol):
    def query(self) -> tuple[bool | None, HardwareStatus]: ...


class ScreenSession:
    def __init__(self, *, store: SettingsBackend, instance: str, sensor: ManagedSensor,
                 switch: SlateSwitch, display: DisplayBackend, context: SessionContext,
                 device: DeviceInfo, slate: SlatePosture | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._stack = ExitStack()
        self.closed = False
        self.context = context
        self.slate = slate
        self._control_lost = False
        self._stack.callback(context.close)
        self._stack.callback(sensor.close)
        if slate is not None:
            self._stack.callback(slate.close)
        qualified = QualifiedDisplay(display, context, slate)
        self.qualified = qualified
        self.hardware_release = HardwareReleaseCoordinator(qualified)
        self._stack.callback(qualified.restore)
        self.available_modes = SCREEN_MODES
        refreshable = isinstance(display, DisplayRefresh)
        self.owner = SessionOwner('windows-screen-session', instance)
        try:
            self.engine = Engine(controller=Controller(input_control=False), sensor=sensor,
                actuator=DisplayOnlyInput(), presentation=PosePresentation(), settings=store,
                instance=instance, device=device, hardware_query=switch.query, monotonic=clock)
            self._stack.callback(self.engine.shutdown)
            self.bridge = SessionBridge(self.engine.state, context.control_available,
                                        getattr(context, 'unattended', lambda: False))
            refresh = (QualifiedRefresh(display, context, lambda: self.engine.state)
                       if refreshable else None)
            self.session = SessionRuntime(self.bridge, qualified, None,
                screen_locked=lambda: not context.control_available(),
                display_refresh=refresh, defer_hardware_handoff=True)
            self._stack.callback(self.session.close)
            self._stack.callback(self.bridge.close)
            self.engine.start()
        except BaseException:
            self._stack.close()
            raise

    def advance(self, now: float) -> None:
        if self.closed:
            return
        self.context.poll()
        if not self.context.control_available() and not self._control_lost:
            self._control_lost = True
            self.engine.reset_after_session_context_loss()
        elif self.context.control_available():
            self._control_lost = False
        self.bridge.drain(self.engine, self.owner)
        self.engine.tick()
        self.bridge.publish(self.engine.state)
        self._prepare_hardware_release()
        self.session.tick(now)

    def _prepare_hardware_release(self) -> None:
        self.hardware_release.prepare(self.engine.state, self.engine.hardware_release_pose)

    def call(self, method: str, params: dict) -> None:
        dispatch_engine_operation(self.engine, method, params)
        self.engine.tick()
        self.bridge.publish(self.engine.state)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.engine.revoke_session(self.owner)
            self._stack.close()


def select_native_display(api, journal, *, hardware_query):
    """Choose a write-capable internal transaction or a read-only docked layout."""
    from .display import ExternalOnlyDisplay, WindowsDisplay, select_external_only, select_panel

    panels = api.panels()
    try:
        select_panel(panels)
    except DisplayError as panel_error:
        try:
            select_external_only(panels)
        except DisplayError as external_error:
            # Multiple/ambiguous internal panels are a different failure than
            # a deliberately docked external-only topology.
            raise (panel_error if external_error.detail == 'internal-panel-still-present'
                   else external_error) from external_error
        if journal.load() is not None:
            raise DisplayError(
                ErrorCode.DISPLAY_UNAVAILABLE,
                'external-only-display-recovery-pending',
            ) from panel_error
        return ExternalOnlyDisplay(api)
    return WindowsDisplay(
        journal=journal, api=api, hardware_query=hardware_query,
    )
