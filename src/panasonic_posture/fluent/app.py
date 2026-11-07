"""Assemble and run the Fluent preview without device or service access."""
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QPixmap, QFont
from PyQt6.QtWidgets import QApplication, QVBoxLayout, QHBoxLayout, QStackedWidget
from qfluentwidgets import (
    FluentWidget, FluentIcon as Icon, TitleLabel, BodyLabel, CaptionLabel,
    PrimaryPushButton, ComboBox, InfoBar, InfoBarPosition, Theme, setTheme,
    setThemeColor, isDarkTheme, qconfig, setCustomStyleSheet, setFont,
)

from ..frontend.ports import UIClient
from .demo import DemoClient, UnavailableClient
from .pages import Home, Settings
from .resources import data_root, translator
from .widgets import (
    WINDOW_LIGHT, WINDOW_DARK, TEXT_LIGHT, TEXT_DARK, MUTED_LIGHT, MUTED_DARK,
    PageSwitcher,
)


class Window(FluentWidget):
    def __init__(self, *, demo=False, language=None, scenario='laptop', client: UIClient | None = None):
        super().__init__()
        _ = translator(language)
        self._ = _
        self.setWindowTitle(_('Panasonic 2-in-1 Tablet Posture Management Tool'))
        self.setMicaEffectEnabled(False)
        self.setCustomBackgroundColor(WINDOW_LIGHT, WINDOW_DARK)
        self.resize(500, 660)
        self.setMinimumSize(420, 560)
        self.titleBar.setFixedHeight(48)
        self.titleBar.iconLabel.setFixedSize(72, 42)
        self.titleBar.iconLabel.setAccessibleName("Let's note")
        self.titleBar.titleLabel.setMinimumWidth(0)
        self.update_mark()
        qconfig.themeChangedFinished.connect(self.update_mark)
        self.client = client if client is not None else DemoClient(QTimer.singleShot) if demo else UnavailableClient()
        if demo and client is None:
            self.client.scenario(scenario)
        self.home = Home(self.client, _, self.notify, self)
        self.settings = Settings(self.client, _, self.notify, self)
        self.stackedWidget = QStackedWidget(self)
        self.stackedWidget.addWidget(self.home)
        self.stackedWidget.addWidget(self.settings)
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
        if demo:
            note = CaptionLabel(_('Demo preview · Changes stay in this window.'))
            note.setWordWrap(True)
            note.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(note)
        for label in self.findChildren((BodyLabel, CaptionLabel, TitleLabel)):
            caption = isinstance(label, CaptionLabel)
            label.setTextColor(MUTED_LIGHT if caption else TEXT_LIGHT,
                               MUTED_DARK if caption else TEXT_DARK)
            setFont(label, 12 if caption else 24 if isinstance(label, TitleLabel) else 13,
                    QFont.Weight.Medium if isinstance(label, TitleLabel) else QFont.Weight.Normal)
        for button in (self.home.confirm_button, self.home.retry_button, self.settings.calibrate_button,
                       self.settings.reset_button, self.settings.findChild(ComboBox)):
            selector = type(button).__name__
            style = f'{selector} {{ border-radius: 10px; }}'
            if isinstance(button, PrimaryPushButton):
                setCustomStyleSheet(button, style, style)
            else:
                setCustomStyleSheet(button, style + f'{selector}:enabled {{color: {TEXT_LIGHT};}}',
                                   style + f'{selector}:enabled {{color: {TEXT_DARK};}}')
        setFont(self.titleBar.titleLabel, 12)
        self.client.start(self.update_state, self.unavailable)

    def update_mark(self):
        name = 'lets-note-window-icon-dark.png' if isDarkTheme() else 'lets-note-window-icon.png'
        path = data_root() / 'assets' / name
        self.setWindowIcon(QIcon(str(path)))
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
        self.stackedWidget.setCurrentWidget(page)
        self.navigation.setCurrentItem(page.objectName())

    def update_state(self, state):
        self.home.update_state(state)
        self.settings.update_state(state)

    def unavailable(self):
        self.home.unavailable()
        self.settings.unavailable()

    def notify(self, success):
        _ = self._
        method = InfoBar.success if success else InfoBar.error
        method(_('Preview updated') if success else _('Unable to complete the operation'),
               _('Demo preview · Changes stay in this window.') if success else _('Please try again.'),
               duration=3500, position=InfoBarPosition.TOP, parent=self)

    def closeEvent(self, event):
        self.unavailable()
        self.client.stop()
        super().closeEvent(event)


def run(*, demo=False, language=None, scenario='laptop') -> int:
    app = QApplication([sys.argv[0]])
    app.setStyle('Fusion')
    app.setApplicationName('Panasonic Posture Manager')
    setTheme(Theme.AUTO)
    setThemeColor('#0051f2')
    window = Window(demo=demo, language=language, scenario=scenario)
    window.show()
    return app.exec()
