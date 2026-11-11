"""Prepare the current display before a physical tablet switch releases."""
import logging
from typing import Protocol

from ..model import StateSnapshot
from ..session.failures import DisplayError


log = logging.getLogger(__name__)


class ReleaseDisplay(Protocol):
    def prepare_hardware_release(self) -> None: ...


class HardwareReleaseCoordinator:
    """Share release preparation without deduplicating the actual re-lock."""

    def __init__(self, display: ReleaseDisplay) -> None:
        self.display = display
        self._key: tuple[str, int, str] | None = None

    def prepare(self, state: StateSnapshot, pose: str | None) -> None:
        if state.hardware is True and pose in ('tent', 'stand'):
            key = (state.instance, state.generation, pose)
            try:
                self.display.prepare_hardware_release()
            except (DisplayError, OSError, ValueError) as error:
                log.warning('cannot prepare for physical tablet release: %s', error)
            else:
                if key != self._key:
                    log.warning('pre-locked the physical tablet direction for pending %s', pose)
                self._key = key
        elif state.hardware is not True:
            self._key = None
