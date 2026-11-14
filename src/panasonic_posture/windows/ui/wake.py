"""Qt wake watch that refreshes the hardware client on service publication.

The service publishes a signal-only named event whenever its published state
signature changes.  This watcher wakes the owner loop, which then issues one
authenticated ``get_state`` through the existing pipe client; no state is
ever read from the event itself.
"""
import logging

from PyQt6 import sip
from PyQt6.QtCore import QObject, QWinEventNotifier

from panasonic_posture.windows.ipc.wake import WakeEvent

log = logging.getLogger(__name__)


class WakeWatch(QObject):
    """Refresh the hardware client when the service publishes a change.

    When the event cannot be opened (service absent, an older installation,
    or a namespace denial) the watcher stays inert and the client's polling
    refresh remains the sole fallback.
    """

    def __init__(self, client, *, wake: WakeEvent | None = None,
                 notifier_factory=QWinEventNotifier,
                 parent: QObject | None = None):
        super().__init__(parent)
        self.client = client
        self._event = wake or WakeEvent()
        self._notifier = None
        try:
            self._event.open()
        except OSError:
            log.debug(
                'hardware wake event is unavailable; polling refresh remains active'
            )
            return
        self._notifier = notifier_factory(
            sip.voidptr(int(self._event.handle)), self
        )
        self._notifier.activated.connect(self._refresh)

    def _refresh(self):
        try:
            self.client.refresh()
        except (OSError, ValueError, RuntimeError):
            # A stopped or superseded client simply has nothing to refresh.
            log.debug('wake refresh rejected', exc_info=True)

    def close(self):
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier.deleteLater()
            self._notifier = None
        self._event.close()


__all__ = ['WakeWatch']
