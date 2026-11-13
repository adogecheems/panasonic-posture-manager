"""Prepare the current display before a physical tablet switch releases."""
import logging
from typing import Protocol

from ..model import StateSnapshot
from ..session.failures import DisplayError


log = logging.getLogger(__name__)


class ReleaseDisplay(Protocol):
    def prepare_hardware_release(self, mode: str) -> None: ...


class HardwareReleaseCoordinator:
    """Share release preparation without deduplicating the actual re-lock."""

    def __init__(self, display: ReleaseDisplay) -> None:
        self.display = display
        self._key: tuple[str, int, str] | None = None
        self._prepared: tuple[str, str] | None = None

    def target_for(self, instance: str) -> str | None:
        """Return a successfully pre-locked target for this service instance."""

        if self._prepared is None or self._prepared[0] != instance:
            return None
        return self._prepared[1]

    def prepare(self, state: StateSnapshot, pose: str | None) -> None:
        if state.hardware is True and pose in ('tent', 'stand'):
            key = (state.instance, state.generation, pose)
            try:
                self.display.prepare_hardware_release(pose)
            except (DisplayError, OSError, ValueError) as error:
                if (
                    isinstance(error, DisplayError)
                    and error.detail
                    in ("physical-tablet-release-changed", "physical-tablet-took-over")
                ):
                    # The switch released between the cached snapshot and the
                    # write. An earlier tick already pre-locked the exact
                    # direction; keep that evidence so the released posture is
                    # applied immediately instead of waiting again.
                    return
                self._key = None
                self._prepared = None
                log.warning('cannot prepare for physical tablet release: %s', error)
            else:
                self._prepared = (state.instance, pose)
                if key != self._key:
                    log.warning('pre-locked the physical tablet direction for pending %s', pose)
                self._key = key
        elif state.hardware is True or state.hardware is None:
            # A hardware re-entry without a fresh successful lock must not
            # inherit the previous release's fast-path evidence.
            self._key = None
            self._prepared = None
