"""Reflect software touch postures through Windows' configured tablet response.

The physical slate indicator remains read-only. This independent shell
session bridge asks Windows to react as it does to the physical slate switch:
respect "do not switch", show Windows' own confirmation prompt, or switch
automatically according to the current user preference.
"""
from __future__ import annotations

import logging
import time

from ..model import StateSnapshot
from .tablet_mode import (
    DESKTOP, PREFERENCE_ALWAYS_SWITCH, PREFERENCE_ASK, PREFERENCE_DONT_SWITCH,
    TABLET, VALID_PREFERENCES, read_prompt_preference,
)

log = logging.getLogger(__name__)
SOFTWARE_TOUCH_MODES = frozenset(('tent', 'stand'))


class WindowsTouchMode:
    """Use the user's Windows slate-switch preference for software postures."""

    def __init__(self, shell, hardware_query, *,
                 preference_query=read_prompt_preference, clock=time.monotonic,
                 retry_seconds: float = 0.5) -> None:
        self.shell, self.hardware_query = shell, hardware_query
        self.preference_query = preference_query
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
                preference = self.preference_query()
            except (OSError, ValueError) as error:
                self._retry_at = self.clock() + self.retry_seconds
                self._restore()
                log.warning('cannot read the Windows slate-mode preference: %s', error)
                return
            if preference not in VALID_PREFERENCES:
                self._retry_at = self.clock() + self.retry_seconds
                self._restore()
                log.warning('unknown Windows slate-mode preference: %r', preference)
                return
            if preference == PREFERENCE_DONT_SWITCH:
                self._restore()
                return
            try:
                current = self.shell.read()
                if self._original is None:
                    self._original = current
                if current != TABLET:
                    if preference == PREFERENCE_ASK:
                        self.shell.prompt_mode(TABLET)
                    elif preference == PREFERENCE_ALWAYS_SWITCH:
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
