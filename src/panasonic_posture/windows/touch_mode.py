"""Keep Windows' touch interaction mode aligned with software postures.

The physical slate indicator remains read-only. This independent shell
session bridge only asks Windows to use its own tablet interaction behavior
for software tent and stand targets, so focus-based touch input behaves like
the physical tablet mode without changing the slate PDO.
"""
from __future__ import annotations

import logging
import time

from ..model import StateSnapshot
from .tablet_mode import DESKTOP, TABLET

log = logging.getLogger(__name__)
SOFTWARE_TOUCH_MODES = frozenset(('tent', 'stand'))


class WindowsTouchMode:
    """Switch the shell only while a software touch posture owns the UI."""

    def __init__(self, shell, hardware_query, *, clock=time.monotonic,
                 retry_seconds: float = 0.5) -> None:
        self.shell, self.hardware_query = shell, hardware_query
        self.clock, self.retry_seconds = clock, float(retry_seconds)
        self._original: int | None = None
        self._requested = False
        self._retry_at = 0.0

    def sync(self, state: StateSnapshot) -> None:
        """Reconcile the shell with a validated engine snapshot."""

        mode = state.target.mode
        if state.hardware is not False:
            return
        if state.posture_pending:
            return
        if mode in SOFTWARE_TOUCH_MODES:
            if (state.error_code or not state.session_connected
                    or not state.supported):
                self._restore()
                return
            if getattr(state.applied, 'mode', None) != mode:
                # Do not switch the shell before the display transaction has
                # confirmed the software posture.
                return
            if self._requested:
                return
            if self.clock() < self._retry_at:
                return
            try:
                current = self.shell.read()
                if self._original is None:
                    self._original = current
                if current != TABLET:
                    self.shell.set_mode(TABLET)
            except (OSError, ValueError) as error:
                self._retry_at = self.clock() + self.retry_seconds
                log.warning('cannot enable Windows touch interaction: %s', error)
            else:
                self._requested = True
                self._retry_at = 0.0
            return

        if self._requested or self._original is not None:
            self._restore()

    def close(self) -> None:
        """Restore the user's shell mode unless physical tablet still owns it."""

        try:
            hardware, _status = self.hardware_query()
        except (OSError, ValueError) as error:
            log.warning('cannot check physical posture while restoring shell mode: %s', error)
            return
        if hardware is True:
            return
        if hardware is False:
            self._restore()

    def _restore(self) -> None:
        if self._original is None and not self._requested:
            return
        try:
            current = self.shell.read()
            if (self._requested and current == TABLET
                    and self._original == DESKTOP):
                self.shell.set_mode(DESKTOP)
        except (OSError, ValueError) as error:
            log.warning('cannot restore Windows shell mode: %s', error)
            return
        self._original = None
        self._requested = False
        self._retry_at = 0.0


__all__ = ['SOFTWARE_TOUCH_MODES', 'WindowsTouchMode']
