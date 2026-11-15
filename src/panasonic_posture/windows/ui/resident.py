"""Native tray lifetime and guarded posture notices.

The tray must not import the Fluent Widgets page tree.  At login it starts with
only PyQt and the hardware client, then constructs the full window on first use.
"""
from collections.abc import Callable
import logging
import time

from PyQt6.QtCore import QObject, QEvent, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QSystemTrayIcon

from ...contracts import TICK_SECONDS
from ...frontend.state import notice_confirmable, notice_token
from .labels import mode_label
from ..app.resources import application_name, asset_root, data_root, translator
from .wake import WakeWatch

log = logging.getLogger(__name__)


def resident_icon():
    """Return the opaque, square application icon shared by Shell surfaces."""
    return QIcon(str(
        asset_root() / 'icons/work.mmoe.panasonic.PostureManager-square.png'
    ))


class ResidentTray(QObject):
    confirmation_failed = pyqtSignal()

    def __init__(self, window=None, *,
                 window_factory: Callable[[], object] | None = None,
                 client=None, activation=None, tray_factory=QSystemTrayIcon,
                 language=None):
        super().__init__(window)
        if window is None and (window_factory is None or client is None):
            raise ValueError('a lazy resident needs a window factory and client')
        self.window = window
        self.window_factory = window_factory
        self.activation = activation
        self.client = window.client if window is not None else client
        self._lazy = window is None
        self._latest_state = None
        self._loading_window = False
        self.app = QApplication.instance()
        self.enabled = tray_factory.isSystemTrayAvailable()
        self.quitting = False
        self._stop_future = None
        self._stop_deadline = None
        self.sent = set()
        self.notice = None
        self._pending_notice = None
        self._notice_click_until = 0.0
        self.instance = None
        self._tooltip = None
        self.icon = resident_icon()
        self.timer = QTimer(self)
        self.activation_timer = QTimer(self)
        self.activation_timer.timeout.connect(self.check_activation)
        self._wake = None
        self._tray_activation = QTimer(self)
        self._tray_activation.setSingleShot(True)
        self._tray_activation.timeout.connect(self._finish_tray_activation)
        self._ = window._ if window is not None else translator(language)
        self._title = (window.windowTitle() if window is not None
                       else application_name(language))
        _ = self._
        self.menu = QMenu(window)
        self.open_action = self.menu.addAction(_('Open app'))
        self.open_action.triggered.connect(self.show_window)
        self.quit_action = self.menu.addAction(_('Quit'))
        self.quit_action.triggered.connect(self.quit)
        self.tray = tray_factory(self.icon, self)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip(self._title)
        self.tray.activated.connect(self.activated)
        self.tray.messageClicked.connect(self.open_notice)
        if window is not None:
            window.state_changed.connect(self.update_state)
            window.connection_lost.connect(self.unavailable)
            window.destroyed.connect(self.window_destroyed)
        else:
            self.confirmation_failed.connect(self._show_confirmation_failure)
            self.timer.timeout.connect(self._poll_client)
            self.client.start(self.update_state, self.unavailable)
            # The tray owns this client until a window exists, so it also owns
            # the wake watch; the window shares the client without another one.
            self._wake = WakeWatch(self.client)
        self.app.aboutToQuit.connect(self.shutdown)
        if self.enabled:
            self.app.setQuitOnLastWindowClosed(False)
            self.tray.show()
        elif window is not None:
            self._add_fallback_note(window)
        if window is not None:
            window.installEventFilter(self)
        if activation is not None:
            self.activation_timer.start(250)
        if self._lazy:
            self.timer.start(max(1, round(TICK_SECONDS * 1000)))

    def _add_fallback_note(self, window):
        note = QLabel(
            self._('The system tray is unavailable. Closing this window will stop the app.'),
            window,
        )
        note.setWordWrap(True)
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        window.layout().addWidget(note)

    def _poll_client(self):
        if self.quitting or self.window is not None:
            return
        try:
            self.client.poll()
        except (OSError, ValueError, RuntimeError):
            log.exception('cannot poll the hardware client from the startup tray')
            self.timer.stop()
            self.unavailable()

    def _ensure_window(self):
        if self.window is not None:
            return True
        if self._loading_window or self.window_factory is None:
            return False
        self._loading_window = True
        self.timer.stop()
        try:
            window = self.window_factory()
        except Exception:
            log.exception('cannot construct the native window')
            if not self.quitting:
                self.timer.start(max(1, round(TICK_SECONDS * 1000)))
                title = self._('Unable to complete the operation')
                detail = self._('Something went wrong. Please try again.')
                if self.enabled:
                    self.tray.showMessage(title, detail, self.icon, 5000)
                else:
                    QMessageBox.critical(None, title, detail)
            return False
        finally:
            self._loading_window = False
        self.window = window
        window.installEventFilter(self)
        window.destroyed.connect(self.window_destroyed)
        if not self.enabled:
            self._add_fallback_note(window)
        if self._latest_state is not None:
            window.update_state(self._latest_state)
        else:
            window.unavailable()
        return True

    def eventFilter(self, watched, event):
        if watched is self.window and event.type() == QEvent.Type.Close and not self.enabled and not self.quitting:
            event.ignore()
            self.quit()
            return True
        if watched is self.window and event.type() == QEvent.Type.Close and self.enabled and not self.quitting:
            event.ignore()
            self.window.hide()
            return True
        return super().eventFilter(watched, event)

    def activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._tray_activation.stop()
            if not self._notice_click_consumed():
                self.show_window()
        elif reason == QSystemTrayIcon.ActivationReason.Trigger:
            # Windows may report one toast click as this tray trigger followed
            # by messageClicked. Delay the tray action so a following notice
            # signal can cancel it; a real tray click still opens the window.
            self._tray_activation.start(100)

    def _finish_tray_activation(self):
        if not self._notice_click_consumed():
            self.show_window()

    def show_window(self):
        if self.quitting or not self._ensure_window():
            return
        self._tray_activation.stop()
        self.window.showNormal()
        self.window.raise_()
        self.window.activateWindow()
        self.window.refresh_startup()

    def open_notice(self):
        """Confirm only when the clicked notice still describes the current state."""
        self._tray_activation.stop()
        if self._notice_click_consumed():
            return
        self._notice_click_until = time.monotonic() + 0.75
        if self.quitting or not self._notice_confirmable():
            self.show_window()
            return
        notice = self.notice
        self.notice = None
        if self._lazy and self.window is None:
            self._pending_notice = notice
            future = self.client.confirm_prompt(notice[2], notice[1])
            future.add_done_callback(self._notice_finished)
            return
        # The page owns busy/error handling and rechecks the same snapshot before
        # sending the epoch-bound confirmation.
        self.window.home.confirm()

    def _notice_finished(self, future):
        try:
            future.result()
        except BaseException:
            notice = getattr(self, '_pending_notice', None)
            if notice_confirmable(self._latest_state, notice):
                self.notice = notice
            self.confirmation_failed.emit()
        finally:
            self._pending_notice = None

    def _show_confirmation_failure(self):
        if not self.quitting:
            self.tray.showMessage(
                self._('Unable to complete the operation'),
                self._('Something went wrong. Please try again.'),
                self.icon,
                5000,
            )

    def _notice_confirmable(self) -> bool:
        if self._lazy and self.window is None:
            return notice_confirmable(self._latest_state, self.notice)
        # A local action already in flight cannot submit another confirmation.
        # Treat that like any other temporarily unconfirmable notice so the
        # click opens the window and leaves the prompt available for retry.
        return (self.window.home.actions.busy is None
                and notice_confirmable(self.window.latest_state, self.notice))

    def _notice_click_consumed(self) -> bool:
        return time.monotonic() < self._notice_click_until

    def check_activation(self):
        try:
            if self.activation.poll():
                self.show_window()
        except OSError:
            log.exception('cannot read native window activation hint')
            self.activation_timer.stop()
            self.show_window()

    def update_state(self, state):
        _ = self._
        self._latest_state = state
        mode = (state.get('applied') or {}).get('mode')
        text = mode_label('tablet' if mode == 'hardware' else mode, _)
        tooltip = self._title + ' · ' + text
        if tooltip != self._tooltip:
            self._tooltip = tooltip
            self.tray.setToolTip(tooltip)
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
            if self._lazy and self.window is not None:
                self.window.update_state(state)
            return
        key = epoch, prompt
        if key not in self.sent:
            # Policy epochs only advance. Keep one latest notice per prompt type;
            # temporarily hidden/unconfirmable prompts must not pop up again.
            self.sent = {old for old in self.sent if old[1] != prompt}
            self.sent.add(key)
            title = _('Tent mode is available') if prompt == 'tent' else _('Stand mode is available')
            self._notice_click_until = 0.0
            self.notice = notice_token(state)
            self.tray.showMessage(title, _('Tap to enable the current posture.'),
                                  self.icon, 15000)
        if self._lazy and self.window is not None:
            self.window.update_state(state)

    def unavailable(self):
        self._latest_state = None
        self.notice = None
        self._notice_click_until = 0.0
        self._tooltip = None
        self.tray.setToolTip(self._title + ' · ' + self._('Connecting to background service…'))
        if self._lazy and self.window is not None:
            self.window.unavailable()

    def shutdown(self):
        if self.quitting:
            return
        self.quitting = True
        self.timer.stop()
        self.activation_timer.stop()
        self._tray_activation.stop()
        self._close_wake()
        self.tray.hide()
        if self.window is not None:
            self.window.shutdown()
        else:
            self.client.stop()

    def _close_wake(self):
        if self._wake is not None:
            self._wake.close()
            self._wake = None

    def quit(self):
        if self.quitting:
            return
        stop = getattr(self.client, 'stop_control', None)
        if stop is not None:
            if self._stop_future is not None:
                return
            if self._stop_deadline is None:
                self._stop_deadline = time.monotonic() + 15.
            self.quit_action.setEnabled(False)
            self._stop_future = stop()
            self._stop_future.add_done_callback(self._control_stopped)
            return
        self._finish_quit()

    def _control_stopped(self, future):
        self._stop_future = None
        if self.quitting:
            return
        try:
            stopped = future.result()
            if not stopped:
                if time.monotonic() >= self._stop_deadline:
                    raise TimeoutError('session stop did not finish')
                QTimer.singleShot(100, self.quit)
                return
        except Exception:
            log.exception('cannot stop posture control')
            self._stop_deadline = None
            self.quit_action.setEnabled(True)
            title = self._('Unable to complete the operation')
            detail = self._('Something went wrong. Please try again.')
            if self.enabled:
                self.tray.showMessage(title, detail, self.icon, 5000)
            else:
                QMessageBox.critical(self.window, title, detail)
            return
        self._finish_quit()

    def _finish_quit(self):
        self.shutdown()
        if self.window is not None:
            self.window.close()
        self.app.quit()

    def window_destroyed(self):
        if not self.quitting:
            self.quitting = True
            self.timer.stop()
            self.activation_timer.stop()
            self._tray_activation.stop()
            self._close_wake()
            self.tray.hide()
            self.client.stop()
        self.app.quit()


__all__ = ['ResidentTray', 'resident_icon']
