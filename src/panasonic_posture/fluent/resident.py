"""Native screen UI lifetime, tray entry and guarded posture notices."""
import logging
import time

from PyQt6.QtCore import QObject, QEvent, QTimer, Qt
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon
from qfluentwidgets import CaptionLabel

from ..frontend.state import notice_confirmable, notice_token
from .pages import mode_labels

log = logging.getLogger(__name__)


class ResidentTray(QObject):
    def __init__(self, window, *, activation=None, tray_factory=QSystemTrayIcon):
        super().__init__(window)
        self.window, self.activation = window, activation
        self.client = window.client
        self.app = QApplication.instance()
        self.enabled = tray_factory.isSystemTrayAvailable()
        self.quitting = False
        self.sent = set()
        self.notice = None
        self._notice_click_until = 0.0
        self.instance = None
        self.timer = QTimer(self)
        self._ = window._
        _ = self._
        self.menu = QMenu(window)
        self.open_action = self.menu.addAction(_('Open app'))
        self.open_action.triggered.connect(self.show_window)
        self.quit_action = self.menu.addAction(_('Quit'))
        self.quit_action.triggered.connect(self.quit)
        self.tray = tray_factory(window.windowIcon(), self)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip(window.windowTitle())
        self.tray.activated.connect(self.activated)
        self.tray.messageClicked.connect(self.open_notice)
        window.state_changed.connect(self.update_state)
        window.connection_lost.connect(self.unavailable)
        self.app.aboutToQuit.connect(self.shutdown)
        window.destroyed.connect(self.window_destroyed)
        if self.enabled:
            window.installEventFilter(self)
            self.app.setQuitOnLastWindowClosed(False)
            self.tray.show()
        else:
            note = CaptionLabel(_('The system tray is unavailable. Closing this window will stop the app.'), window)
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            window.layout().addWidget(note)
        if activation is not None:
            self.timer.timeout.connect(self.check_activation)
            self.timer.start(250)

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close and self.enabled and not self.quitting:
            event.ignore()
            self.window.hide()
            return True
        return super().eventFilter(watched, event)

    def activated(self, reason):
        if (reason == QSystemTrayIcon.ActivationReason.Trigger
                and self._notice_confirmable()):
            self.open_notice()
        elif reason in (QSystemTrayIcon.ActivationReason.Trigger,
                        QSystemTrayIcon.ActivationReason.DoubleClick):
            if not self._notice_click_consumed():
                self.show_window()

    def show_window(self):
        if self.quitting:
            return
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        self.window.settings.refresh_startup()

    def open_notice(self):
        """Confirm only when the clicked notice still describes the current state."""
        if self.quitting or not self._notice_confirmable():
            if self._notice_click_consumed():
                return
            self.show_window()
            return
        self.notice = None
        # Windows can deliver a toast click as both messageClicked and a tray
        # activation. Confirm once and suppress the paired signal; a separate
        # later tray click still opens the window normally.
        self._notice_click_until = time.monotonic() + 0.75
        # The page owns busy/error handling and rechecks the same snapshot before
        # sending the epoch-bound confirmation.
        self.window.home.confirm()

    def _notice_confirmable(self) -> bool:
        # A local action already in flight cannot submit another confirmation.
        # Treat that like any other temporarily unconfirmable notice so the
        # click opens the window and leaves the prompt available for retry.
        return (self.window.home.actions.busy is None
                and notice_confirmable(self.window.settings.model.state, self.notice))

    def _notice_click_consumed(self) -> bool:
        return time.monotonic() < self._notice_click_until

    def check_activation(self):
        try:
            if self.activation.poll():
                self.show_window()
        except OSError:
            log.exception('cannot read native window activation hint')
            self.timer.stop()
            self.show_window()

    def update_state(self, state):
        _ = self._
        mode = (state.get('applied') or {}).get('mode')
        text = mode_labels(_).get('tablet' if mode == 'hardware' else mode,
                                  _('Waiting for the desktop to get ready…'))
        self.tray.setToolTip(self.window.windowTitle() + ' · ' + text)
        if state['instance'] != self.instance:
            self.instance = state['instance']
            self.sent.clear()
        epoch, prompt = state['pending_notification'], state['pending_prompt']
        if (not self.enabled or not self.tray.supportsMessages() or not state['supported'] or
                not state['session_connected'] or state['error_code'] or state['hardware'] is not False or
                epoch is None or prompt not in ('tent', 'stand')):
            # Keep the token across temporary unconfirmable snapshots. A
            # toast already handed to Windows may still be clicked in Action
            # Center after the prompt briefly disappears and returns with the
            # same epoch. _notice_confirmable() still validates the current
            # snapshot before sending anything.
            return
        key = epoch, prompt
        if key in self.sent:
            return
        # Policy epochs only advance. Keep one latest notice per prompt type;
        # temporarily hidden/unconfirmable prompts must not pop up again.
        self.sent = {old for old in self.sent if old[1] != prompt}
        self.sent.add(key)
        title = _('Tent mode is available') if prompt == 'tent' else _('Stand mode is available')
        self._notice_click_until = 0.0
        self.notice = notice_token(state)
        self.tray.showMessage(title, _('Tap to enable the current posture.'),
                              QSystemTrayIcon.MessageIcon.Information, 15000)

    def unavailable(self):
        self.notice = None
        self._notice_click_until = 0.0
        self.tray.setToolTip(self.window.windowTitle() + ' · ' + self._('Connecting to background service…'))

    def shutdown(self):
        if self.quitting:
            return
        self.quitting = True
        self.timer.stop()
        self.tray.hide()
        self.window.shutdown()

    def quit(self):
        self.shutdown()
        self.window.close()
        self.app.quit()

    def window_destroyed(self):
        if not self.quitting:
            self.quitting = True
            self.timer.stop()
            self.tray.hide()
            self.client.stop()
        self.app.quit()
