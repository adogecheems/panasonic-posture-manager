"""Ordinary-user screen control using the shared engine and session runtime.

The engine stays on the backend owner thread. Display workers exchange bounded
requests with it; they never mutate the engine or manufacture input receipts.
This route never constructs privileged hardware ports or opens their pipe.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from contextlib import ExitStack
from dataclasses import replace
import queue
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Protocol

from ..contracts import ErrorCode
from ..controller import Controller
from ..frontend.ports import PREFERENCES
from ..model import DeviceInfo, HardwareStatus, SensorInfo, SensorReading, StateSnapshot
from ..presentation import PosePresentation
from ..service.engine import Engine
from ..service.ports import SensorBackend, SessionOwner, SettingsBackend
from ..session.failures import DisplayError, FailureKind, ServiceError
from ..session.runtime import SessionRuntime
from ..session.ports import DisplayBackend
from .display_only_input import DisplayOnlyInput
from .orientation import OrientationReading
from .session_display import (
    QualifiedDisplay, QualifiedRefresh, SessionContext, SlatePosture,
    slate_qualified_hardware,
)

SCREEN_MODES = ('auto', 'laptop', 'tent', 'stand')


def available_screen_modes(display: object) -> tuple[str, ...]:
    del display
    return SCREEN_MODES


class ManagedSensor(SensorBackend, Protocol):
    def start(self) -> None: ...
    def close(self) -> None: ...


class SlateSwitch(Protocol):
    def query(self) -> tuple[bool | None, HardwareStatus]: ...


class SessionBridge:
    """Private in-process capability, not an externally callable receipt API."""

    def __init__(self, state: StateSnapshot, available: Callable[[], bool],
                 unattended: Callable[[], bool] = lambda: False) -> None:
        self._lock = threading.Lock()
        self._state, self.available = state, available
        self.unattended = unattended
        self._requests: queue.Queue[tuple[Future[None], tuple[int, bool, str | None, str | None]]] = queue.Queue(maxsize=4)
        self._accepted: list[Future[None]] = []
        self._closed = False

    @staticmethod
    def rejected() -> ServiceError:
        return ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, 'screen-session-unavailable', kind=FailureKind.REJECTED)

    def publish(self, state: StateSnapshot) -> None:
        with self._lock:
            self._state = state
            accepted, self._accepted = self._accepted, []
        # SessionRuntime probes by reading immediately after a receipt. The
        # response must not race ahead of the owner tick's accepted snapshot.
        for future in accepted:
            if not future.done():
                future.set_result(None)

    def get_state(self) -> StateSnapshot:
        with self._lock:
            if self._closed or not self.available():
                raise self.rejected()
            return self._state

    def session_update(self, generation: int, ready: bool, error_code: str | None = None,
                       error_detail: str | None = None) -> None:
        future: Future[None] = Future()
        with self._lock:
            if self._closed or not self.available():
                raise self.rejected()
            try:
                self._requests.put_nowait((future, (generation, ready, error_code, error_detail)))
            except queue.Full as error:
                raise self.rejected() from error
        try:
            future.result(timeout=1.)
        except TimeoutError as error:
            # Cancellation prevents an old queued receipt renewing a lease.
            future.cancel()
            raise self.rejected() from error

    def drain(self, engine: Engine, owner: SessionOwner) -> None:
        for _ in range(4):
            try:
                future, args = self._requests.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if self._closed or not self.available() or not engine.session_update(
                        *args, owner=owner, unattended=self.unattended()):
                    raise self.rejected()
            except Exception as error:
                future.set_exception(error)
            else:
                self._accepted.append(future)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            accepted, self._accepted = self._accepted, []
            for future in accepted:
                if not future.done():
                    future.set_exception(self.rejected())
            while True:
                try:
                    future, _ = self._requests.get_nowait()
                except queue.Empty:
                    break
                if not future.done():
                    future.set_exception(self.rejected())


class ScreenSession:
    def __init__(self, *, store: SettingsBackend, instance: str, sensor: ManagedSensor,
                 switch: SlateSwitch, display: DisplayBackend, context: SessionContext,
                 device: DeviceInfo, slate: SlatePosture | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._stack = ExitStack()
        self.closed = False
        self.context = context
        self._stack.callback(context.close)
        self._stack.callback(sensor.close)
        if slate is not None:
            self._stack.callback(slate.close)
        qualified = QualifiedDisplay(display, context, slate)
        self._stack.callback(qualified.restore)
        self.available_modes = available_screen_modes(display)
        refreshable = callable(getattr(display, 'needs_refresh', None)) and callable(
            getattr(display, 'refresh', None)
        )
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
                screen_locked=lambda: not context.control_available(), display_refresh=refresh)
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
        if not self.context.control_available():
            self.engine.revoke_session(self.owner)
        self.bridge.drain(self.engine, self.owner)
        self.engine.tick()
        self.bridge.publish(self.engine.state)
        self.session.tick(now)

    def call(self, method: str, params: dict) -> None:
        if method == 'set_mode':
            mode = params['mode']
            if not isinstance(mode, str) or mode not in self.available_modes:
                from ..service.engine import ServiceError as EngineError
                raise EngineError(ErrorCode.INVALID_MODE, 'screen-mode-unavailable')
            self.engine.set_mode(mode)
        elif method == 'retry':
            fallback = params['fallback_mode']
            if fallback is not None:
                if not isinstance(fallback, str) or fallback not in self.available_modes:
                    raise ValueError('invalid fallback mode')
                self.engine.set_mode(fallback)
            self.engine.retry()
        elif method == 'set_preference':
            field, enabled = params['field'], params['enabled']
            if not isinstance(field, str) or field not in PREFERENCES or type(enabled) is not bool:
                raise ValueError('invalid preference')
            getattr(self.engine, 'set_' + field)(enabled)
        elif method == 'confirm_prompt':
            prompt, epoch = params['prompt'], params['epoch']
            if prompt not in ('tent', 'stand') or type(epoch) is not int or epoch < 0:
                raise ValueError('invalid prompt')
            getattr(self.engine, 'confirm_' + prompt)(epoch)
        elif method == 'calibrate':
            self.engine.calibrate_sensor()
        elif method == 'reset_calibration':
            self.engine.reset_sensor_calibration()
        else:
            raise ValueError('unknown method')
        self.engine.tick()
        self.bridge.publish(self.engine.state)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.engine.revoke_session(self.owner)
            self._stack.close()


class ProfileSensor:
    """Do not apply the known mount to a different sensor model."""

    def __init__(self, sensor: ManagedSensor) -> None:
        self.sensor = sensor

    @property
    def info(self) -> SensorInfo:
        info = self.sensor.info
        if ((info.ready or info.device) and
                not info.device.casefold().startswith('\\\\?\\hid#vid_8087&pid_0ac2#')):
            return replace(info, ready=False, error='sensor-mount-profile-mismatch')
        return info

    def reading(self) -> SensorReading:
        return self.sensor.reading() if self.info.ready else SensorReading(source='windows-winrt')

    def orientation(self) -> OrientationReading | None:
        return self.sensor.orientation() if self.info.ready else None

    def start(self) -> None:
        self.sensor.start()

    def close(self) -> None:
        self.sensor.close()


def require_native_user() -> None:
    from .security import WindowsSecurity
    security = WindowsSecurity()
    security.require_unprivileged()
    identity = security.process_identity(os.getpid())
    console = security.kernel.WTSGetActiveConsoleSessionId()
    if console in (0, 0xffffffff) or identity.session_id != console:
        raise PermissionError('native screen control requires the local console session')


def verified_machine() -> tuple[str, str]:
    """Read identity only; capability checks stay in the native session factory."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\BIOS') as key:
        vendor = winreg.QueryValueEx(key, 'SystemManufacturer')[0]
        product = winreg.QueryValueEx(key, 'SystemProductName')[0]
    if (vendor, product) != ('Panasonic Corporation', 'CFQV8-1L'):
        raise OSError('no verified Windows posture mount for this machine')
    return vendor, product


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


def native_screen_session(store: SettingsBackend, instance: str, journal_path: Path) -> ScreenSession:
    """Select only the machine/mount with collected Windows pose evidence."""
    from ..session.journal import Journal
    from .convertible_slate import NativeSlateDevice, SlateNotifier
    from .display import NativeDisplay
    from .slate_journal import SlateJournal, lease_path
    from .sensor import WindowsSensor, IDENTITY
    from .session_context import WindowsSessionContext
    from .switch import WindowsSwitch
    require_native_user()
    vendor, product = verified_machine()
    with ExitStack() as resources:
        context = WindowsSessionContext()
        resources.callback(context.close)
        switch = WindowsSwitch()
        slate = SlateNotifier(
            NativeSlateDevice(), SlateJournal(lease_path(journal_path))
        )
        sensor = ProfileSensor(WindowsSensor(mount_matrix=IDENTITY, include_orientation=False))
        resources.callback(sensor.close)
        journal = Journal(journal_path)
        api = NativeDisplay()
        hardware_query = slate_qualified_hardware(
            switch.query, lambda: slate.software_owned,
            lambda: slate.software_settling,
        )
        display = select_native_display(
            api, journal, hardware_query=hardware_query,
        )
        session = ScreenSession(store=store, instance=instance,
            sensor=sensor, switch=SimpleNamespace(query=hardware_query),
            display=display, context=context, slate=slate,
            device=DeviceInfo(id='windows-cfqv8-screen', vendor=vendor, product=product,
                              platform='windows', support_tier='compatible'))
        resources.pop_all()  # ScreenSession now owns all three resources.
        return session
