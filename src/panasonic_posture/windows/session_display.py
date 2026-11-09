"""Nonprivileged display qualification shared by both Windows compositions."""
from typing import Protocol

from ..contracts import ErrorCode
from ..model import Target
from ..session.failures import DisplayError
from ..session.ports import DisplayBackend


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
