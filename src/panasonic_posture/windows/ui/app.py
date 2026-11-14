"""Assemble the Windows Fluent UI around the authenticated hardware client."""
import sys

from PyQt6.QtCore import Qt, QTimer, QEasingCurve, QPropertyAnimation, pyqtSignal
from PyQt6.QtGui import QIcon, QPixmap, QFont
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QHBoxLayout, QStackedWidget, QGraphicsOpacityEffect, QMessageBox
from qfluentwidgets import (
    FluentWidget, FluentIcon as Icon, TitleLabel, BodyLabel, CaptionLabel,
    PrimaryPushButton, ComboBox, InfoBar, InfoBarPosition, isDarkTheme, qconfig,
    setCustomStyleSheet, setFont,
    setFontFamilies,
)

from ...contracts import TICK_SECONDS
from ...frontend.ports import UIClient
from .unavailable import UnavailableClient
from ..ipc.client import HardwareUIClient
from .pages import Home, Settings
from ..app.resources import application_name, asset_root, data_root, translator
from .wake import WakeWatch
from .widgets import (
    WINDOW_LIGHT, WINDOW_DARK, TEXT_LIGHT, TEXT_DARK, MUTED_LIGHT, MUTED_DARK,
    PageSwitcher, apply_app_font, BODY_SIZE, CAPTION_SIZE, HEADING_SIZE,
    SECTION_TITLE_SIZE, EMPHASIS_WEIGHT, restyle_notice_bar,
)


def configure_application_font():
    """Use the platform UI font with its native antialiasing.

    Full grid-fitting hinting renders CJK glyphs with visibly stepped edges
    on Windows. Leave hinting at the platform default so DirectWrite keeps
    its ClearType antialiasing, and ask for antialiased glyphs explicitly.
    """
    app = QApplication.instance()
    if app is None:
        return
    font = QFont(app.font())
    families = [family for family in font.families() if family]
    if families:
        setFontFamilies(families)
    font.setHintingPreference(QFont.HintingPreference.PreferDefaultHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    app.setFont(font)


def configure_application(language=None):
    """Apply the localized shell name before native windows are created."""
    app = QApplication.instance()
    if app is None:
        raise RuntimeError('QApplication must exist before configuring the application name')
    name = application_name(language)
    app.setApplicationName(name)
    app.setApplicationDisplayName(name)
    return name


def _prefers_reduced_motion(hints) -> bool:
    """Read the accessibility preference when the Qt API exposes it."""
    accessibility = getattr(hints, 'accessibility', None)
    motion_preference = getattr(Qt, 'MotionPreference', None)
    if accessibility is None or motion_preference is None:
        # Qt 6.8 and other older Qt 6 releases predate the accessibility API.
        return False
    return accessibility().motionPreference() == motion_preference.ReducedMotion


class Window(FluentWidget):
    state_changed = pyqtSignal(dict)
    connection_lost = pyqtSignal()

    def __init__(self, *, language=None, client: UIClient | None = None,
                 startup=None, update_checker=None, start_client: bool = True):
        if type(start_client) is not bool:
            raise ValueError('client startup ownership must be explicit')
        configure_application_font()
        super().__init__()
        _ = translator(language)
        self._ = _
        # Destroy the Qt object tree while the event loop can still process
        # deferred deletion, before PyQt's interpreter-exit wrapper cleanup.
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(application_name(language))
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
        self._stopped = False
        if client is not None:
            self.client = client
        elif sys.platform == 'win32':
            self.client = HardwareUIClient()
        else:
            self.client = UnavailableClient()
        self.home = Home(self.client, _, self.notify, self)
        self._startup = startup
        self._update_checker = update_checker
        self._settings = None
        self.stackedWidget = QStackedWidget(self)
        self.stackedWidget.addWidget(self.home)
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
        self._style_labels()
        for button in (self.home.confirm_button, self.home.retry_button, self.home.auto_button):
            self._style_button(button)
        setFont(self.titleBar.titleLabel, BODY_SIZE)
        self._last_state = None
        self._wake = None
        if start_client:
            self.client.start(self.update_state, self.unavailable)
        self._backend_poll_count = 0
        self.backend_timer = QTimer(self)
        if isinstance(self.client, HardwareUIClient):
            self.backend_timer.timeout.connect(self.poll_backend)
            # Match the shared service cadence instead of waking twice as often.
            self.backend_timer.start(max(1, round(TICK_SECONDS * 1000)))
            if start_client:
                # Only the owner of the client watches the wake event.  The
                # resident tray owns it when the window shares its client.
                self._wake = WakeWatch(self.client, parent=self)

    def _style_labels(self):
        for label in self.findChildren((BodyLabel, CaptionLabel, TitleLabel)):
            caption = isinstance(label, CaptionLabel)
            label.setTextColor(MUTED_LIGHT if caption else TEXT_LIGHT,
                               MUTED_DARK if caption else TEXT_DARK)
            setFont(label,
                    CAPTION_SIZE if caption else HEADING_SIZE if isinstance(label, TitleLabel) else BODY_SIZE,
                    EMPHASIS_WEIGHT if isinstance(label, TitleLabel) else QFont.Weight.Normal)

    @staticmethod
    def _style_button(button):
        selector = type(button).__name__
        style = f'{selector} {{ border-radius: 10px; }}'
        if isinstance(button, PrimaryPushButton):
            setCustomStyleSheet(button, style, style)
        else:
            setCustomStyleSheet(button, style + f'{selector}:enabled {{color: {TEXT_LIGHT};}}',
                               style + f'{selector}:enabled {{color: {TEXT_DARK};}}')

    @property
    def settings(self):
        """Build the settings page on first use.

        The resident shell opens the window without paying the settings page
        construction cost; the page is created only when navigation or a
        caller actually needs it.
        """
        if self._settings is None:
            settings = Settings(
                self.client, self._, self.notify, self,
                startup=self._startup, update_checker=self._update_checker,
            )
            self.stackedWidget.addWidget(settings)
            self._settings = settings
            self._style_labels()
            for button in (settings.calibrate_button, settings.reset_button,
                           settings.update_button, settings.findChild(ComboBox)):
                self._style_button(button)
            if self._last_state is not None:
                settings.update_state(self._last_state)
            else:
                settings.unavailable()
        return self._settings

    @property
    def latest_state(self):
        """The newest validated snapshot without forcing the settings page."""
        return self._last_state

    def refresh_startup(self):
        if self._settings is not None:
            self._settings.refresh_startup()

    def poll_backend(self):
        self.client.poll()
        self._backend_poll_count += 1
        interval = max(1, round(TICK_SECONDS * 1000))
        if self._backend_poll_count * interval >= 1000:
            self._backend_poll_count = 0
            # The service wake event publishes changes immediately; this
            # bounded read-only refresh repairs a single dropped publication
            # and keeps the visible result convergent with the backend.
            self.client.refresh()

    def update_mark(self):
        name = 'lets-note-window-icon-dark.png' if isDarkTheme() else 'lets-note-window-icon.png'
        path = asset_root() / 'branding' / name
        self.setWindowIcon(QIcon(str(asset_root() / 'icons/work.mmoe.panasonic.PostureManager.png')))
        mark = QPixmap(str(path))
        # Qt 6.12 scales QPixmap while loading, but Qt 6.8 leaves the source at
        # 1x. Normalize both paths to the current screen's physical pixels.
        ratio = self.devicePixelRatioF()
        if ratio <= 1.0:
            screen = self.screen() or QApplication.primaryScreen()
            if screen is not None:
                ratio = screen.devicePixelRatio()
        ratio = max(1.0, ratio)
        target_width = max(1, round(72 * ratio))
        if mark.width() != target_width:
            mark = mark.scaledToWidth(
                target_width, Qt.TransformationMode.SmoothTransformation
            )
        mark.setDevicePixelRatio(ratio)
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
        self.page_opacity.setEnabled(
            not _prefers_reduced_motion(QApplication.styleHints())
        )
        if self.page_opacity.isEnabled():
            self.page_fade.start()

    def update_state(self, state):
        if state == self._last_state:
            return
        self._last_state = state
        self.home.update_state(state)
        if self._settings is not None:
            self._settings.update_state(state)
        self.state_changed.emit(state)

    def unavailable(self):
        self._last_state = None
        self.home.unavailable()
        if self._settings is not None:
            self._settings.unavailable()
        self.connection_lost.emit()

    def notify(self, success, detail: str | None = None):
        _ = self._
        if success:
            bar = InfoBar.success(_('Settings saved'), '', duration=3500,
                                  position=InfoBarPosition.TOP, parent=self)
        else:
            bar = InfoBar.error(_('Unable to complete the operation'), detail or _('Please try again.'),
                                duration=3500, position=InfoBarPosition.TOP, parent=self)
        apply_app_font(bar.titleLabel, SECTION_TITLE_SIZE, EMPHASIS_WEIGHT)
        apply_app_font(bar.contentLabel, BODY_SIZE)
        if isinstance(bar, InfoBar):
            # Widget tests stub the banner factories with plain labels;
            # only real banners carry the geometry this restyle manages.
            restyle_notice_bar(bar)

    def shutdown(self):
        if self._stopped:
            return
        self._stopped = True
        self.backend_timer.stop()
        self.unavailable()
        if self._settings is not None:
            self._settings.shutdown()
        if self._wake is not None:
            self._wake.close()
        self.client.stop()

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


def run(*, language=None, background=False) -> int:
    from .launcher import run as run_application
    return run_application(language=language, background=background)


def hardware_start_failure(language, reason: str) -> int:
    """Explain a preflight rejection without starting a backend or touching state."""
    if reason != 'session':
        raise ValueError('unknown hardware startup failure')
    _ = translator(language)
    detail = _('Run from the active local desktop with a normal user account.')
    app = QApplication.instance() or QApplication([sys.argv[0]])
    QMessageBox.critical(None, _('Hardware mode is unavailable'), detail)
    return 1
