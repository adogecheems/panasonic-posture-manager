"""Assemble Fluent with explicitly selected preview, screen or service clients."""
import sys

from PyQt6 import sip
from PyQt6.QtCore import Qt, QTimer, QEasingCurve, QPropertyAnimation, pyqtSignal, QCoreApplication, QEvent
from PyQt6.QtGui import QIcon, QPixmap, QFont
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QHBoxLayout, QStackedWidget, QGraphicsOpacityEffect, QMessageBox
from qfluentwidgets import (
    FluentWidget, FluentIcon as Icon, TitleLabel, BodyLabel, CaptionLabel,
    PrimaryPushButton, ComboBox, InfoBar, InfoBarPosition, Theme, setTheme,
    setThemeColor, isDarkTheme, qconfig, setCustomStyleSheet, setFont,
    setFontFamilies,
)

from ..contracts import TICK_SECONDS
from ..frontend.ports import UIClient
from .demo import DemoClient, UnavailableClient
from ..windows.client import ProcessClient, settings_path as default_settings_path
from ..windows.hardware_client import HardwareUIClient
from .pages import Home, Settings
from .resources import data_root, translator
from .widgets import (
    WINDOW_LIGHT, WINDOW_DARK, TEXT_LIGHT, TEXT_DARK, MUTED_LIGHT, MUTED_DARK,
    PageSwitcher,
)


def configure_application_font():
    """Use the platform UI font for both Latin and CJK text."""
    app = QApplication.instance()
    if app is None:
        return
    font = QFont(app.font())
    families = [family for family in font.families() if family]
    if families:
        setFontFamilies(families)
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)


class Window(FluentWidget):
    state_changed = pyqtSignal(dict)
    connection_lost = pyqtSignal()

    def __init__(self, *, demo=False, language=None, scenario='laptop', client: UIClient | None = None,
                 backend=False, simulate_backend=False, settings_path=None, hardware_backend=False, native_backend=False,
                 startup=None):
        if hardware_backend and (demo or backend or simulate_backend or native_backend or settings_path is not None):
            raise ValueError('hardware backend cannot be combined with software backend options')
        if native_backend and (demo or backend or simulate_backend):
            raise ValueError('native backend cannot be combined with preview backend options')
        configure_application_font()
        super().__init__()
        _ = translator(language)
        self._ = _
        # Destroy the Qt object tree while the event loop can still process
        # deferred deletion, before PyQt's interpreter-exit wrapper cleanup.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(_('Panasonic 2-in-1 Tablet Posture Management Tool'))
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(WINDOW_LIGHT, WINDOW_DARK)
        self.resize(500, 660)
        self.setMinimumSize(420, 560)
        self.titleBar.setFixedHeight(48)
        self.titleBar.iconLabel.setFixedSize(72, 42)
        self.titleBar.iconLabel.setAccessibleName("Let's note")
        self.titleBar.titleLabel.setMinimumWidth(0)
        # Keep a little breathing room between the wordmark and window title.
        self.titleBar.hBoxLayout.insertSpacing(1, 6)
        # Keep the text and mark on one visual center while allowing a small
        # vertical correction for CJK font metrics.
        self.titleBar.titleLabel.setContentsMargins(0, 1, 0, 4)
        self.update_mark()
        qconfig.themeChangedFinished.connect(self.update_mark)
        self.simulating = demo or simulate_backend
        self.memory_demo = demo
        self._stopped = False
        if client is not None:
            self.client = client
        elif demo:
            self.client = DemoClient(QTimer.singleShot)
        elif hardware_backend:
            self.client = HardwareUIClient()
        elif backend or simulate_backend or native_backend or sys.platform == 'win32':
            path = settings_path if settings_path is not None else default_settings_path(simulate=simulate_backend, native=native_backend)
            self.client = ProcessClient(path, simulate=simulate_backend, scenario=scenario, native=native_backend)
        else:
            self.client = UnavailableClient()
        if demo and client is None:
            self.client.scenario(scenario)
        self.home = Home(self.client, _, self.notify, self)
        self.settings = Settings(self.client, _, self.notify, self, startup=startup)
        self.stackedWidget = QStackedWidget(self)
        self.stackedWidget.addWidget(self.home)
        self.stackedWidget.addWidget(self.settings)
        self.page_opacity = QGraphicsOpacityEffect(self.stackedWidget)
        self.stackedWidget.setGraphicsEffect(self.page_opacity)
        self.page_opacity.setEnabled(False)
        self.page_fade = QPropertyAnimation(self.page_opacity, b'opacity', self)
        self.page_fade.setDuration(150)
        self.page_fade.setStartValue(0.65)
        self.page_fade.setEndValue(1.0)
        self.page_fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.page_fade.finished.connect(lambda: self.page_opacity.setEnabled(False))
        self.navigation = PageSwitcher(self)
        self.navigation.addItem('posture', _('Current posture'), lambda: self.switchTo(self.home), Icon.HOME)
        self.navigation.addItem('settings', _('Settings'), lambda: self.switchTo(self.settings), Icon.SETTING)
        self.navigation.fit_items()
        self.navigation.setCurrentItem('posture')
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 48, 0, 12)
        layout.setSpacing(8)
        layout.addWidget(self.stackedWidget, 1)
        footer = QHBoxLayout()
        footer.setContentsMargins(20, 0, 20, 0)
        footer.addStretch()
        footer.addWidget(self.navigation)
        footer.addStretch()
        layout.addLayout(footer)
        if demo or simulate_backend:
            note = CaptionLabel(_('Demo preview · Changes stay in this window.') if demo else
                                _('Simulation · Preferences are saved separately.'))
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(note)
        for label in self.findChildren((BodyLabel, CaptionLabel, TitleLabel)):
            caption = isinstance(label, CaptionLabel)
            label.setTextColor(MUTED_LIGHT if caption else TEXT_LIGHT,
                               MUTED_DARK if caption else TEXT_DARK)
            setFont(label, 12 if caption else 24 if isinstance(label, TitleLabel) else 13,
                    QFont.Weight.Medium if isinstance(label, TitleLabel) else QFont.Weight.Normal)
        for button in (self.home.confirm_button, self.home.retry_button, self.home.auto_button, self.settings.calibrate_button,
                       self.settings.reset_button, self.settings.findChild(ComboBox)):
            selector = type(button).__name__
            style = f'{selector} {{ border-radius: 10px; }}'
            if isinstance(button, PrimaryPushButton):
                setCustomStyleSheet(button, style, style)
            else:
                setCustomStyleSheet(button, style + f'{selector}:enabled {{color: {TEXT_LIGHT};}}',
                                   style + f'{selector}:enabled {{color: {TEXT_DARK};}}')
        setFont(self.titleBar.titleLabel, 13)
        self.client.start(self.update_state, self.unavailable)
        self._backend_poll_count = 0
        self.backend_timer = QTimer(self)
        if isinstance(self.client, (ProcessClient, HardwareUIClient)):
            self.backend_timer.timeout.connect(self.poll_backend)
            # Match the shared service cadence instead of waking twice as often.
            self.backend_timer.start(max(1, round(TICK_SECONDS * 1000)))

    def poll_backend(self):
        self.client.poll()
        self._backend_poll_count += 1
        interval = max(1, round(TICK_SECONDS * 1000))
        if self._backend_poll_count * interval >= 1000:
            self._backend_poll_count = 0
            # Push delivery normally publishes changes immediately. A bounded
            # read-only refresh repairs a single dropped state notification and
            # keeps the visible result convergent with the backend.
            self.client.refresh()

    def update_mark(self):
        name = 'lets-note-window-icon-dark.png' if isDarkTheme() else 'lets-note-window-icon.png'
        path = data_root() / 'assets' / name
        self.setWindowIcon(QIcon(str(data_root() / 'assets' / 'work.mmoe.panasonic.PostureManager.png')))
        mark = QPixmap(str(path))
        # Keep every source pixel. Qt samples the original at the destination
        # screen's scale instead of enlarging an already downsampled 1x image.
        mark.setDevicePixelRatio(mark.width() / 72)
        self.titleBar.iconLabel.setPixmap(mark)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Keep the full accessible/OS title; elide only the narrow header text.
        label = self.titleBar.titleLabel
        label.setMaximumWidth(max(60, self.width() - 248))
        label.setText(label.fontMetrics().elidedText(self.windowTitle(), Qt.TextElideMode.ElideRight,
                                                    label.maximumWidth()))

    def switchTo(self, page):
        if page is self.stackedWidget.currentWidget():
            return
        self.page_fade.stop()
        self.stackedWidget.setCurrentWidget(page)
        self.navigation.setCurrentItem(page.objectName())
        motion = QApplication.styleHints().accessibility().motionPreference()
        self.page_opacity.setEnabled(motion != Qt.MotionPreference.ReducedMotion)
        if self.page_opacity.isEnabled():
            self.page_fade.start()

    def update_state(self, state):
        self.home.update_state(state)
        self.settings.update_state(state)
        self.state_changed.emit(state)

    def unavailable(self):
        self.home.unavailable()
        self.settings.unavailable()
        self.connection_lost.emit()

    def notify(self, success, detail: str | None = None):
        _ = self._
        if success:
            title = _('Preview updated') if self.simulating else _('Settings saved')
            message = (_('Demo preview · Changes stay in this window.') if self.memory_demo else
                       _('Simulation · Preferences are saved separately.') if self.simulating else '')
            InfoBar.success(title, message, duration=3500,
                            position=InfoBarPosition.TOP, parent=self)
        else:
            InfoBar.error(_('Unable to complete the operation'), detail or _('Please try again.'),
                          duration=3500, position=InfoBarPosition.TOP, parent=self)

    def shutdown(self):
        if self._stopped:
            return
        self._stopped = True
        self.backend_timer.stop()
        self.unavailable()
        self.client.stop()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


def run(*, demo=False, language=None, scenario='laptop', backend=False,
        simulate_backend=False, settings_path=None, hardware_backend=False, native_backend=False, background=False) -> int:
    activation = None
    startup = None
    if native_backend:
        from ..windows.screen_session import require_native_user, verified_machine
        try:
            require_native_user()  # UI activation/startup never runs with an elevated token.
            verified_machine()
        except PermissionError:
            return native_start_failure(language, 'session')
        except OSError:
            return native_start_failure(language, 'machine')
        from ..windows.activation import WindowActivation
        from ..windows.startup import LoginStartup, startup_command, UnavailableStartup
        activation = WindowActivation(default_settings_path(native=True).parent)
        if not activation.acquire(background=background):
            return 0
        try:
            startup = LoginStartup(startup_command()) if settings_path is None else UnavailableStartup()
        except (OSError, ValueError):
            startup = UnavailableStartup()
    try:
        app = QApplication([sys.argv[0]])
        app.setStyle('Fusion')
        app.setApplicationName('Panasonic Posture Manager')
        setTheme(Theme.AUTO)
        setThemeColor('#0051f2')
        window = Window(demo=demo, language=language, scenario=scenario, backend=backend,
                        simulate_backend=simulate_backend, settings_path=settings_path, hardware_backend=hardware_backend,
                        native_backend=native_backend, startup=startup)
        resident = None
        try:
            if native_backend:
                from .resident import ResidentTray
                resident = ResidentTray(window, activation=activation)
            if not background or resident is None or not resident.enabled:
                window.show()
            return app.exec()
        finally:
            if resident is not None:
                resident.shutdown()
            else:
                window.shutdown()
            if not sip.isdeleted(window):
                window.close()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    finally:
        if activation is not None:
            activation.close()


def native_start_failure(language, reason: str) -> int:
    """Explain a preflight rejection without starting a backend or touching state."""
    if reason not in ('session', 'machine'):
        raise ValueError('unknown native startup failure')
    _ = translator(language)
    detail = (_('Native mode requires the active local desktop and a normal user account.')
              if reason == 'session' else
              _('This device does not have a verified Windows sensor and display configuration. Use preview mode or check the supported models in the maintenance guide.'))
    app = QApplication.instance() or QApplication([sys.argv[0]])
    QMessageBox.critical(None, _('Native mode is unavailable'), detail)
    return 1
