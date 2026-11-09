"""Nonprivileged display qualification shared by both Windows compositions."""
from collections.abc import Callable
from typing import Protocol

from ..contracts import ErrorCode
from ..model import HardwareStatus, StateSnapshot, Target
from ..session.failures import DisplayError
from ..session.ports import DisplayBackend, DisplayRefresh


class SessionContext(Protocol):
    def control_available(self) -> bool: ...
    def poll(self) -> None: ...
    def close(self) -> None: ...


class QualifiedDisplay:
    def __init__(self, display: DisplayBackend, context: SessionContext) -> None:
        self.display, self.context = display, context

    def _require_available(self) -> None:
        if not self.context.control_available():
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'native session control is unavailable')

    def apply(self, target: Target) -> None:
        if target.mode == 'unavailable':
            self.display.apply(target)  # Existing ownership may still restore.
            return
        self._require_available()
        self.display.apply(target)
        if not self.context.control_available():
            self.display.restore()
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'session changed during display apply')

    def hold_orientation(self) -> None:
        self._require_available()
        self.display.hold_orientation()
        if not self.context.control_available():
            self.display.restore()
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'session changed during display hold')

    def restore(self, normal: bool = False) -> None:
        self.display.restore(normal=normal)


class QualifiedRefresh:
    """Fence ongoing writes against current local qualification and target."""

    def __init__(self, display: DisplayRefresh, context: SessionContext,
                 current: Callable[[], StateSnapshot]) -> None:
        self.display, self.context, self.current = display, context, current

    def needs_refresh(self, target: Target) -> bool:
        return self.display.needs_refresh(target)

    def _require_current(self, expected: StateSnapshot) -> StateSnapshot:
        if not self.context.control_available():
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'native-session-control-unavailable')
        current = self.current()
        if (current.instance != expected.instance or current.generation != expected.generation or
                current.target != expected.target or not current.session_connected or
                not current.supported or current.sensor_error or current.error_code or
                current.hardware is not False or current.hardware_status != HardwareStatus.OK):
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'ongoing-display-target-no-longer-current')
        return current

    def refresh(self, state: StateSnapshot) -> None:
        current = self._require_current(state)
        self.display.refresh(current)
        self._require_current(state)
