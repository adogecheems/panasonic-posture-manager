"""Fluent desktop views over the shared UI ports; no device or service access."""
import math
import sys
import time

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QIcon, QPainter
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QButtonGroup
from qfluentwidgets import (
    FluentWindow, FluentIcon as Icon, NavigationItemPosition, CardWidget,
    TitleLabel, SubtitleLabel, BodyLabel, CaptionLabel, StrongBodyLabel,
    PrimaryPushButton, PushButton, RadioButton, SwitchButton, ComboBox,
    ScrollArea, InfoBar, InfoBarPosition, MessageBox, Theme, setTheme,
    setThemeColor, isDarkTheme, qconfig,
)

from .. import __version__
from ..frontend.actions import ActionRunner, SettingsModel
from ..frontend.ports import UIClient
from ..frontend.state import PostureModel, Detail
from .demo import DemoClient, UnavailableClient
from .resources import data_root, translator


def mode_labels(_):
    return {'auto': _('Automatic'), 'laptop': _('Laptop'), 'tent': _('Tent'),
            'stand': _('Stand'), 'tablet': _('Tablet')}


class Illustration(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pose = 'laptop'
        self.setMinimumHeight(220)
        self.setMaximumHeight(270)
        qconfig.themeChanged.connect(self.update)

    def paintEvent(self, event):
        path = data_root() / 'assets' / f'{self.pose}.svg'
        dark = isDarkTheme()
        svg = path.read_text(encoding='utf-8').replace('currentColor', '#e8e7ef' if dark else '#383549')
        svg = svg.replace('var(--app-bg, white)', '#28272f' if dark else '#ffffff')
        renderer = QSvgRenderer(svg.encode())
        painter = QPainter(self)
        width = min(self.width(), self.height() * 4 / 3)
        renderer.render(painter, QRectF((self.width() - width) / 2, 0, width, width * 3 / 4))


class Page(ScrollArea):
    def __init__(self, name, title, subtitle, demo, _, parent=None):
        super().__init__(parent)
        self.setObjectName(name)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet('QScrollArea {background: transparent; border: none;}')
        content = QWidget()
        content.setObjectName(name + 'Content')
        content.setStyleSheet(f'#{name}Content {{background: transparent;}}')
        self.setWidget(content)
        self.box = QVBoxLayout(content)
        self.box.setContentsMargins(28, 24, 28, 28)
        self.box.setSpacing(16)
        self.box.addWidget(TitleLabel(title))
        description = BodyLabel(subtitle)
        description.setWordWrap(True)
        self.box.addWidget(description)
        note = BodyLabel(_('Demo preview · Changes stay in this window.') if demo else
                         _('Windows preview · The backend is not connected yet.'))
        note.setWordWrap(True)
        note.setStyleSheet('color: #8c72df; padding: 10px 12px; border: 1px solid #b8a4ee; border-radius: 8px;')
        self.box.addWidget(note)

    def card(self):
        card = CardWidget()
        layout = QVBoxLayout(card)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        self.box.addWidget(card)
        return card, layout


class Home(Page):
    def __init__(self, client: UIClient, demo, _, notify, choose_mode, parent=None):
        super().__init__('posture', _('Current posture'),
                         _('A comfortable view, in every position.'), demo, _, parent)
        self._, self.client = _, client
        self.model = PostureModel()
        self.labels = mode_labels(_)
        self.actions = ActionRunner(self.sync_buttons, lambda error: notify(False), client.refresh)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.show_slow)
        card, box = self.card()
        self.illustration = Illustration()
        box.addWidget(self.illustration)
        self.heading = TitleLabel(self.labels['laptop'])
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.heading)
        self.detail = BodyLabel(_('Connecting to background service…'))
        self.detail.setWordWrap(True)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.detail)
        self.confirm_button = PrimaryPushButton(_('Enter tent mode'))
        self.confirm_button.clicked.connect(self.confirm)
        self.retry_button = PushButton(Icon.SYNC, _('Retry'))
        self.retry_button.clicked.connect(self.retry)
        self.confirm_button.hide()
        self.retry_button.hide()
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.retry_button)
        buttons.addStretch()
        box.addLayout(buttons)
        card, box = self.card()
        row = QHBoxLayout()
        for title, attr in ((_('Screen orientation'), 'rotation'), (_('Built-in keyboard and touchpad'), 'input_state')):
            column = QVBoxLayout()
            column.addWidget(CaptionLabel(title))
            label = StrongBodyLabel(_('Unavailable'))
            setattr(self, attr, label)
            column.addWidget(label)
            row.addLayout(column, 1)
        box.addLayout(row)
        choose = PushButton(Icon.SETTING, _('Choose mode'))
        choose.clicked.connect(choose_mode)
        box.addWidget(choose, alignment=Qt.AlignmentFlag.AlignLeft)
        if demo:
            card, box = self.card()
            box.addWidget(StrongBodyLabel(_('Preview scenarios')))
            scenarios = ComboBox()
            for key, text in (
                ('laptop', _('Laptop')), ('prompt', _('Tent confirmation')),
                ('hardware', _('Physical tablet mode')), ('slow', _('Slow switching')),
                ('error', _('Switch failure')), ('disconnected', _('Disconnected')),
            ):
                scenarios.addItem(text, userData=key)
            scenarios.currentIndexChanged.connect(lambda index: client.scenario(scenarios.itemData(index)))
            box.addWidget(scenarios)
        self.box.addStretch()

    def update_state(self, state):
        old_deadline = self.model.deadline
        self.model.update(state, time.monotonic())
        if old_deadline != self.model.deadline:
            self.timer.stop()
            if self.model.deadline is not None:
                self.timer.start(max(1, math.ceil((self.model.deadline - time.monotonic()) * 1000)))
        self.illustration.pose = self.model.pose
        self.illustration.setAccessibleName(self.labels[self.model.pose])
        self.illustration.update()
        self.heading.setText(self.labels[self.model.pose])
        self.render_detail()
        self.confirm_button.setVisible(state['pending_notification'] is not None)
        self.retry_button.setVisible(bool(state['error_code']))
        applied = (state.get('applied') or {}).get('mode')
        if applied in ('tent', 'laptop', 'stand', 'tablet', 'hardware'):
            self.rotation.setText(self._('Upside down') if applied == 'tent' else
                                  self._('Automatic rotation') if applied in ('tablet', 'hardware') else self._('Landscape'))
            self.input_state.setText(self._('Enabled') if applied == 'laptop' else self._('Disabled'))
        else:
            self.rotation.setText(self._('Unavailable'))
            self.input_state.setText(self._('Unavailable'))
        self.sync_buttons()

    def render_detail(self):
        _ = self._
        messages = {
            Detail.NONE: '', Detail.DISCONNECTED: _('Windows preview · The backend is not connected yet.'),
            Detail.ERROR: _('Mode change failed'), Detail.SESSION: _('Waiting for the desktop to get ready…'),
            Detail.UNSUPPORTED: _('This model does not support automatic mode changes.'),
            Detail.SWITCH: _('The tablet switch is unavailable. Restart the background service if this continues.'),
            Detail.SENSOR: _('No posture data is available. Keep the device still; switching will resume automatically.'),
            Detail.SLOW: _('Switching is taking longer than usual. Please wait or choose another mode in Settings.'),
            Detail.COMPATIBLE: _('Compatibility mode: this model is supported but has not been individually validated.'),
        }
        self.detail.setText(messages[self.model.detail])
        self.detail.setVisible(bool(self.detail.text()))

    def show_slow(self):
        self.model.advance(time.monotonic())
        # A callback must not consume the deadline if the platform wakes early.
        if self.model.deadline is not None and not self.model.slow:
            self.timer.start(max(1, math.ceil((self.model.deadline - time.monotonic()) * 1000)))
        self.render_detail()

    def unavailable(self):
        self.timer.stop()
        self.actions.disconnect()
        self.model.disconnect()
        self.render_detail()
        self.confirm_button.hide()
        self.retry_button.hide()
        self.rotation.setText(self._('Unavailable'))
        self.input_state.setText(self._('Unavailable'))

    def sync_buttons(self):
        prompt = self.model.state and self.model.state['pending_prompt']
        self.confirm_button.setText(self._('Enter stand mode') if prompt == 'stand' else self._('Enter tent mode'))
        self.confirm_button.setEnabled(self.actions.busy is None)
        self.retry_button.setEnabled(self.actions.busy is None)

    def confirm(self):
        state = self.model.state
        if state and state['pending_notification'] is not None:
            self.actions.run('confirm', lambda: self.client.confirm_prompt(state['pending_prompt'], state['pending_notification']))

    def retry(self):
        if self.model.state:
            self.actions.run('retry', lambda: self.client.retry(self.model.state['manual']))


class ModeRadio(RadioButton):
    """Arrow keys browse without activating a mode that may disable input."""

    def keyPressEvent(self, event):
        steps = {Qt.Key.Key_Up: -1, Qt.Key.Key_Left: -1,
                 Qt.Key.Key_Down: 1, Qt.Key.Key_Right: 1}
        if event.key() in steps:
            buttons = [button for button in self.group().buttons() if button.isEnabled()]
            if self in buttons:
                buttons[(buttons.index(self) + steps[event.key()]) % len(buttons)].setFocus()
            event.accept()
            return
        super().keyPressEvent(event)


class Settings(Page):
    def __init__(self, client: UIClient, demo, _, notify, parent=None):
        super().__init__('settings', _('Settings'), _('Make each posture work for you.'), demo, _, parent)
        self._, self.client, self.notify = _, client, notify
        self.model = SettingsModel(client, self.render, lambda error: notify(False))
        self.calibration = ActionRunner(self.render_calibration, lambda error: notify(False), self.calibrated)
        self.switches, self.radios = {}, {}
        card, box = self.card()
        box.addWidget(SubtitleLabel(_('Mode selection')))
        self.group = QButtonGroup(self)
        descriptions = {
            'auto': _('Switch automatically to suit the current posture'),
            'laptop': _('Best for typing and precise work'),
            'tent': _('Turns the screen upside down and disables the built-in keyboard and touchpad'),
            'stand': _('Keeps the screen upright and disables the built-in keyboard and touchpad'),
            'tablet': _('Lets the screen rotate and disables the built-in keyboard and touchpad'),
        }
        for key, label in mode_labels(_).items():
            radio = ModeRadio(label)
            radio.setAccessibleName(label)
            self.group.addButton(radio)
            radio.clicked.connect(lambda checked, mode=key: self.model.queue('manual', mode))
            self.radios[key] = radio
            box.addWidget(radio)
            caption = CaptionLabel(descriptions[key])
            caption.setWordWrap(True)
            box.addWidget(caption)
        card, box = self.card()
        box.addWidget(SubtitleLabel(_('Automatic mode')))
        for field, title, subtitle in (
            ('tent_enabled', _('Enable tent mode'), _('Allow automatic detection of tent mode')),
            ('tent_prompt', _('Tent mode notification'), _('Ask before switching to tent mode when the base is upright')),
            ('stand_prompt', _('Stand mode notification'), _('Ask before switching to stand mode when the base is folded back')),
        ):
            row = QHBoxLayout()
            column = QVBoxLayout()
            column.addWidget(StrongBodyLabel(title))
            caption = CaptionLabel(subtitle)
            caption.setWordWrap(True)
            column.addWidget(caption)
            row.addLayout(column, 1)
            switch = SwitchButton()
            switch.setOnText(_('Enabled'))
            switch.setOffText(_('Disabled'))
            switch.setAccessibleName(title)
            switch.checkedChanged.connect(lambda enabled, f=field: self.model.queue(f, enabled))
            self.switches[field] = switch
            row.addWidget(switch)
            box.addLayout(row)
        card, box = self.card()
        box.addWidget(SubtitleLabel(_('Sensor calibration')))
        self.calibration_hint = BodyLabel(_('Place the device in laptop form on a flat desk, then calibrate.'))
        self.calibration_hint.setWordWrap(True)
        box.addWidget(self.calibration_hint)
        row = QHBoxLayout()
        self.calibrate_button = PushButton(Icon.SYNC, _('Calibrate'))
        self.calibrate_button.clicked.connect(self.ask_calibration)
        self.reset_button = PushButton(_('Reset'))
        self.reset_button.clicked.connect(lambda: self.calibration.run('reset', self.client.reset_calibration))
        row.addWidget(self.calibrate_button)
        row.addWidget(self.reset_button)
        row.addStretch()
        box.addLayout(row)
        self.box.addStretch()
        self.unavailable()

    def update_state(self, state):
        self.model.update(state)
        self.render()

    def render(self):
        if self.model.state is None:
            return
        for key, radio in self.radios.items():
            radio.setChecked(self.model.value('manual') == key)
            radio.setEnabled(self.model.mode_enabled(key))
        for field, switch in self.switches.items():
            switch.blockSignals(True)
            switch.setChecked(self.model.value(field))
            switch.blockSignals(False)
            switch.setEnabled(self.model.preference_enabled(field))
        self.render_calibration()

    def render_calibration(self):
        state = self.model.state
        enabled = bool(state and state['supported'] and self.calibration.busy is None)
        self.calibrate_button.setEnabled(enabled and state['hardware'] is not True)
        self.reset_button.setEnabled(enabled)
        calibrated = bool(state and state['sensor_calibrated'])
        self.reset_button.setVisible(calibrated)
        self.calibration_hint.setText(self._('A custom flat-desk reference is in use.') if calibrated else
                                      self._('Place the device in laptop form on a flat desk, then calibrate.'))

    def calibrated(self):
        self.notify(True)
        self.client.refresh()

    def ask_calibration(self):
        _ = self._
        connection = self.model.connection
        dialog = MessageBox(_('Calibrate sensor?'),
                            _('Place the device in laptop form on a flat, stable desk and wait about two seconds before confirming. Its current direction will become the level reference.'), self.window())
        dialog.yesButton.setText(_('Calibrate'))
        dialog.cancelButton.setText(_('Cancel'))
        if dialog.exec() and connection == self.model.connection:
            self.calibration.run('calibrate', self.client.calibrate)

    def unavailable(self):
        self.model.disconnect()
        self.calibration.disconnect()
        for widget in (*self.radios.values(), *self.switches.values(), self.calibrate_button, self.reset_button):
            widget.setEnabled(False)


class Window(FluentWindow):
    def __init__(self, *, demo=False, language=None, client: UIClient | None = None):
        super().__init__()
        _ = translator(language)
        self._ = _
        self.setWindowTitle(_('Panasonic 2-in-1 Tablet Posture Management Tool'))
        self.setWindowIcon(QIcon(str(data_root() / 'assets/work.mmoe.panasonic.PostureManager.png')))
        self.resize(1000, 820)
        self.setMinimumSize(760, 620)
        self.navigationInterface.setExpandWidth(190)
        self.navigationInterface.setMinimumExpandWidth(800)
        self.client = client if client is not None else DemoClient(QTimer.singleShot) if demo else UnavailableClient()
        self.home = Home(self.client, demo, _, self.notify, lambda: self.switchTo(self.settings), self)
        self.settings = Settings(self.client, demo, _, self.notify, self)
        self.about = Page('about', _('About'), _('Panasonic 2-in-1 Tablet Posture Management Tool'), demo, _, self)
        card, box = self.about.card()
        box.addWidget(SubtitleLabel(_('Version') + ' ' + __version__))
        box.addWidget(BodyLabel(_('Author') + ' · adogecheems'))
        notice = BodyLabel(_("Let's note is a trademark of Panasonic Corporation. The author does not claim any rights to the trademark and is not associated with it."))
        notice.setWordWrap(True)
        box.addWidget(notice)
        card, box = self.about.card()
        box.addWidget(StrongBodyLabel(_('Appearance')))
        appearance = ComboBox()
        for text in (_('System'), _('Light'), _('Dark')):
            appearance.addItem(text)
        appearance.currentIndexChanged.connect(lambda index: setTheme((Theme.AUTO, Theme.LIGHT, Theme.DARK)[index]))
        box.addWidget(appearance)
        self.about.box.addStretch()
        self.addSubInterface(self.home, Icon.HOME, _('Current posture'))
        self.addSubInterface(self.settings, Icon.SETTING, _('Settings'))
        self.addSubInterface(self.about, Icon.INFO, _('About'), position=NavigationItemPosition.BOTTOM)
        self.client.start(self.update_state, self.unavailable)

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


def run(*, demo=False, language=None) -> int:
    app = QApplication([sys.argv[0]])
    app.setStyle('Fusion')
    app.setApplicationName('Panasonic Posture Manager')
    setTheme(Theme.AUTO)
    setThemeColor('#7861d8')
    window = Window(demo=demo, language=language)
    window.show()
    return app.exec()
