"""Tent-mode prompt notifications with an explicit shown/retry state machine.

The daemon publishes ``pending_notification`` (the policy epoch of the current
tent prompt).  A prompt may only be suppressed for an epoch after the
notification server *accepted* the notification; otherwise a transient
notification failure or a local screen lock would permanently hide the
prompt (P2-15).  Failures are retried with a 1/2/4 second backoff; after the
last retry the epoch is marked as failed and is not retried again until the
epoch changes or :meth:`close` is called (for example after unlocking).
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from gi.repository import Gio, GLib

from .. import protocol
from . import errors
from .errors import describe_error

log = logging.getLogger(__name__)

NOTIFY_DEST = "org.freedesktop.Notifications"
NOTIFY_PATH = "/org/freedesktop/Notifications"
NOTIFY_IFACE = NOTIFY_DEST

#: Delay before each retry; three retries means four attempts for one epoch.
RETRY_DELAYS = (1.0, 2.0, 4.0)
DEFAULT_TIMEOUT_MS = 1200
ICON_NAME = "work.mmoe.panasonic.PostureManager"


class Notifier:
    """Show/close the tent-mode prompt and remember which epoch was shown.

    ``session_bus`` and the clock are injectable so the tests never talk to a
    real desktop notification server.
    """

    def __init__(
        self,
        session_bus=None,
        gettext: Callable[[str], str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        desktop_entry: str = protocol.NAME + "Manager",
    ):
        self._bus = session_bus
        self._ = gettext or errors.get_translator()
        self._clock = clock
        self.timeout_ms = int(timeout_ms)
        self.desktop_entry = desktop_entry

        #: Notification server id of the prompt currently on screen (0 = none).
        self.notification = 0
        #: Epoch the on-screen prompt refers to (``None`` = no prompt shown).
        self.epoch: int | None = None
        #: Epoch whose notification was successfully displayed.
        self.shown_epoch: int | None = None

        self._pending_epoch: int | None = None
        self._retries = 0
        self._retry_at: float | None = None
        self._failed_epoch: int | None = None
        self._capabilities: list[str] | None = None

    # -- public API -------------------------------------------------------

    @property
    def session_bus(self):
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    def sync(
        self,
        epoch: int | None,
        allow_retry: bool = True,
        now: float | None = None,
    ) -> None:
        """Converge the on-screen prompt towards *epoch*.

        * ``epoch is None`` closes any prompt (no tent candidate).
        * A successfully shown epoch is never shown twice.
        * Failed attempts retry after 1/2/4 s; after the last retry the epoch
          is abandoned until it changes or :meth:`close` is called.
        """

        now = self._clock() if now is None else now
        if epoch is None:
            self.close()
            return
        epoch = int(epoch)
        if epoch == self.shown_epoch:
            return
        if epoch == self._failed_epoch:
            return
        if self._pending_epoch != epoch:
            # A new prompt replaces whatever is on screen and resets backoff.
            self._pending_epoch = epoch
            self._retries = 0
            self._retry_at = None
            if self.notification and self.epoch != epoch:
                self._close_notification()
        if self._retry_at is not None:
            if not allow_retry or now < self._retry_at:
                return
        self._attempt(epoch, now)

    def close(self) -> None:
        """Close the prompt and forget every token/epoch so it can reappear."""

        self._close_notification()
        self.epoch = None
        self.shown_epoch = None
        self._pending_epoch = None
        self._retries = 0
        self._retry_at = None
        self._failed_epoch = None
        self._capabilities = None

    # -- internals --------------------------------------------------------

    def _attempt(self, epoch: int, now: float) -> None:
        try:
            notification = self._notify()
        except (GLib.Error, OSError, ValueError, errors.SessionError) as error:
            log.warning("tent notification failed: %s", describe_error(error))
            self.notification = 0
            self.epoch = None
            self._capabilities = None
            if self._retries >= len(RETRY_DELAYS):
                # Give up for this epoch: a new epoch or close() retries again.
                self._failed_epoch = epoch
                self._pending_epoch = None
                self._retries = 0
                self._retry_at = None
            else:
                self._retry_at = now + RETRY_DELAYS[self._retries]
                self._retries += 1
            return
        self.notification = int(notification or 0)
        self.epoch = epoch
        self.shown_epoch = epoch
        self._pending_epoch = None
        self._retries = 0
        self._retry_at = None
        self._failed_epoch = None

    def _notify(self) -> int:
        capabilities = self._get_capabilities()
        actions = (
            ["default", self._("Enable tent mode")] if "actions" in capabilities else []
        )
        hints = {"desktop-entry": GLib.Variant("s", self.desktop_entry)}
        parameters = GLib.Variant(
            "(susssasa{sv}i)",
            (
                self._("Panasonic 2-in-1 Tablet Posture Management Tool"),
                0,
                ICON_NAME,
                self._("Tent mode"),
                self._("Tap to enable tent mode"),
                actions,
                hints,
                -1,
            ),
        )
        reply = self._call("Notify", parameters)
        value = reply.unpack()[0]
        return int(value or 0)

    def _get_capabilities(self) -> list[str]:
        if self._capabilities is None:
            reply = self._call("GetCapabilities", None)
            self._capabilities = [str(item) for item in reply.unpack()[0]]
        return self._capabilities

    def _close_notification(self) -> None:
        identifier = self.notification
        self.notification = 0
        self.epoch = None
        if not identifier:
            return
        try:
            self._call("CloseNotification", GLib.Variant("(u)", (int(identifier),)))
        except (GLib.Error, OSError, ValueError) as error:
            log.debug("cannot close notification %s: %s", identifier, describe_error(error))

    def _call(self, method: str, parameters):
        return self.session_bus.call_sync(
            NOTIFY_DEST,
            NOTIFY_PATH,
            NOTIFY_IFACE,
            method,
            parameters,
            None,
            Gio.DBusCallFlags.NONE,
            self.timeout_ms,
            None,
        )


__all__ = [
    "DEFAULT_TIMEOUT_MS",
    "NOTIFY_DEST",
    "NOTIFY_IFACE",
    "NOTIFY_PATH",
    "Notifier",
    "RETRY_DELAYS",
]
