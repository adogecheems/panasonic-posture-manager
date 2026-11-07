"""Posture and settings pages over the shared frontend models."""
import math
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon, QFont
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QButtonGroup
from qfluentwidgets import (
    FluentIcon as Icon, SettingCardGroup, SettingCard,
    TitleLabel, BodyLabel, CaptionLabel,
    PrimaryPushButton, PushButton, ComboBox, MessageBox,
    Theme, setTheme, isDarkTheme, qconfig, setCustomStyleSheet, setFont,
)

from .. import __version__
from ..frontend.actions import ActionRunner, SettingsModel
from ..frontend.ports import UIClient
from ..frontend.state import PostureModel, Detail
from .widgets import (
    WINDOW_LIGHT, WINDOW_DARK, TEXT_LIGHT, TEXT_DARK, MUTED_LIGHT, MUTED_DARK,
    Illustration, Page, ModeRadio, SettingRow, SwitchRow, row_style,
)


def mode_labels(_):
    return {'auto': _('Automatic'), 'laptop': _('Laptop'), 'tent': _('Tent'),
            'stand': _('Stand'), 'tablet': _('Tablet')}


class Home(Page):
    def __init__(self, client: UIClient, _, notify, parent=None):
        super().__init__('posture', parent)
        self._, self.client = _, client
        self.model = PostureModel()
        self.labels = mode_labels(_)
        self.actions = ActionRunner(self.sync_buttons, lambda error: notify(False), client.refresh)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.show_slow)
        box = self.box
        box.setSpacing(6)
        box.addStretch()
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
        self.auto_button = PushButton(_('Return to automatic'))
        self.auto_button.clicked.connect(self.return_auto)
        self.auto_button.hide()
        self.confirm_button.hide()
        self.retry_button.hide()
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.retry_button)
        buttons.addWidget(self.auto_button)
        buttons.addStretch()
        box.addLayout(buttons)
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
        self.auto_button.setVisible(self.model.can_return_auto)
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
        self.auto_button.hide()

    def sync_buttons(self):
        prompt = self.model.state and self.model.state['pending_prompt']
        self.confirm_button.setText(self._('Enter stand mode') if prompt == 'stand' else self._('Enter tent mode'))
        self.confirm_button.setEnabled(self.actions.busy is None)
        self.retry_button.setEnabled(self.actions.busy is None)
        self.auto_button.setEnabled(self.actions.busy is None)

    def confirm(self):
        state = self.model.state
        if state and state['pending_notification'] is not None:
            self.actions.run('confirm', lambda: self.client.confirm_prompt(state['pending_prompt'], state['pending_notification']))

    def return_auto(self):
        if self.model.can_return_auto:
            self.actions.run('auto', lambda: self.client.set_mode('auto'))

    def retry(self):
        if self.model.state:
            self.actions.run('retry', lambda: self.client.retry(self.model.state['manual']))


class Settings(Page):
    def __init__(self, client: UIClient, _, notify, parent=None):
        super().__init__('settings', parent)
        self._, self.client, self.notify = _, client, notify
        self.model = SettingsModel(client, self.render, lambda error: notify(False))
        self.calibration = ActionRunner(self.render_calibration, lambda error: notify(False), self.calibrated)
        self.switches, self.radios = {}, {}
        self.cards = []
        self.groups = []
        self.box.setContentsMargins(24, 18, 24, 20)
        self.box.setSpacing(22)
        group = self.add_group(_('Automatic mode'))
        self.switch_cards = {}
        for field, title, subtitle in (
            ('tent_enabled', _('Detect tent mode automatically'), _('You can still select tent mode manually when this is off.')),
            ('tent_prompt', _('Tent mode notification'), _('Ask before switching to tent mode when the base is upright')),
            ('stand_prompt', _('Stand mode notification'), _('Ask before switching to stand mode when the base is folded back')),
        ):
            card = SwitchRow(QIcon(), title, subtitle)
            switch = card.switchButton
            switch.label.hide()
            switch.hBox.setContentsMargins(0, 0, 0, 0)
            switch.setFixedWidth(switch.indicator.width())
            switch.setAccessibleName(title)
            card.checkedChanged.connect(lambda enabled, f=field: self.model.queue(f, enabled))
            self.switch_cards[field] = card
            self.switches[field] = switch
            self.add_card(group, card, (switch,))
        group = self.add_group(_('Sensor'))
        self.sensor_card = SettingRow(QIcon(), _('Sensor calibration'),
                                      _('Place the device in laptop form on a flat desk, then calibrate.'))
        self.calibration_hint = self.sensor_card.contentLabel
        self.calibrate_button = PushButton(_('Calibrate'))
        self.calibrate_button.clicked.connect(self.ask_calibration)
        self.reset_button = PushButton(_('Reset'))
        self.reset_button.clicked.connect(lambda: self.calibration.run('reset', self.client.reset_calibration))
        self.sensor_card.hBoxLayout.addWidget(self.reset_button)
        self.sensor_card.hBoxLayout.addSpacing(6)
        self.sensor_card.hBoxLayout.addWidget(self.calibrate_button)
        self.sensor_card.hBoxLayout.addSpacing(16)
        self.add_card(group, self.sensor_card, (self.reset_button, self.calibrate_button))
        group = self.add_group(_('Mode selection'))
        self.group = QButtonGroup(self)
        descriptions = {
            'auto': _('Switch automatically to suit the current posture'),
            'laptop': _('Best for typing and precise work'),
            'tent': _('Turns the screen upside down and disables the built-in keyboard and touchpad'),
            'stand': _('Keeps the screen upright and disables the built-in keyboard and touchpad'),
            'tablet': _('Lets the screen rotate and disables the built-in keyboard and touchpad'),
        }
        for key, label in mode_labels(_).items():
            card = SettingRow(QIcon(), label, descriptions[key])
            radio = ModeRadio()
            radio.setAccessibleName(label)
            self.group.addButton(radio)
            radio.clicked.connect(lambda checked, mode=key: self.model.queue('manual', mode))
            self.radios[key] = radio
            card.hBoxLayout.insertWidget(0, radio)
            card.hBoxLayout.setContentsMargins(16, 0, 0, 0)
            self.add_card(group, card, (radio,))
        group = self.add_group(_('About'))
        self.add_card(group, SettingRow(QIcon(), _('Version'), __version__))
        self.add_card(group, SettingRow(QIcon(), _('Author'), 'adogecheems'))
        appearance = ComboBox()
        for text in (_('System'), _('Light'), _('Dark')):
            appearance.addItem(text)
        appearance.setAccessibleName(_('Appearance'))
        appearance.currentIndexChanged.connect(lambda index: setTheme((Theme.AUTO, Theme.LIGHT, Theme.DARK)[index]))
        card = SettingRow(QIcon(), _('Appearance'))
        card.hBoxLayout.addWidget(appearance)
        card.hBoxLayout.addSpacing(16)
        self.add_card(group, card, (appearance,))
        notice = CaptionLabel(_("Let's note is a trademark of Panasonic Corporation. The author does not claim any rights to the trademark and is not associated with it."))
        notice.setWordWrap(True)
        for group in self.groups:
            rows = group.findChildren(SettingCard, options=Qt.FindChildOption.FindDirectChildrenOnly)
            rows[0].setProperty('first', True)
            rows[-1].setProperty('last', True)
        self.box.addWidget(notice)
        self.box.addStretch()
        self.scroll_edge = QFrame(self.viewport())
        self.scroll_edge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.tint_scroll_edge()
        qconfig.themeChangedFinished.connect(self.tint_scroll_edge)
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self.update_scroll_edge)
        bar.rangeChanged.connect(self.update_scroll_edge)
        self.unavailable()

    def add_group(self, title):
        group = SettingCardGroup(title)
        group.cardLayout.setSpacing(0)
        group.titleLabel.setContentsMargins(4, 0, 0, 0)
        setFont(group.titleLabel, 13, QFont.Weight.Medium)
        setCustomStyleSheet(group, f'SettingCardGroup > QLabel {{color: {MUTED_LIGHT};}}',
                           f'SettingCardGroup > QLabel {{color: {MUTED_DARK};}}')
        self.groups.append(group)
        self.box.addWidget(group)
        return group

    def add_card(self, group, card, controls=()):
        card.iconLabel.hide()
        # These rows follow the icon-free Linux layout; remove the native
        # icon-to-text spacer as well as the unused icon itself.
        spacer = card.hBoxLayout.itemAt(card.hBoxLayout.indexOf(card.iconLabel) + 1).spacerItem()
        spacer.changeSize(0, 0)
        card.titleLabel.setObjectName('settingTitle')
        card.titleLabel.setWordWrap(True)
        card.contentLabel.setWordWrap(True)
        card.vBoxLayout.setSpacing(3)
        setCustomStyleSheet(card,
            row_style('#ffffff', '#e1e5eb', TEXT_LIGHT, MUTED_LIGHT),
            row_style('#2b3038', '#393f49', TEXT_DARK, MUTED_DARK))
        self.cards.append((card, controls))
        group.addSettingCard(card)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.scroll_edge.setGeometry(0, self.viewport().height() - 14, self.viewport().width(), 14)
        QTimer.singleShot(0, self.fit_cards)

    def tint_scroll_edge(self):
        color = WINDOW_DARK if isDarkTheme() else WINDOW_LIGHT
        self.scroll_edge.setStyleSheet(
            'border: none; background: qlineargradient(x1:0, y1:0, x2:0, y2:1, '
            f'stop:0 transparent, stop:1 {color});')

    def update_scroll_edge(self):
        bar = self.verticalScrollBar()
        self.scroll_edge.setVisible(bar.value() < bar.maximum())

    def fit_cards(self):
        for card, controls in self.cards:
            reserve = sum(control.sizeHint().width() for control in controls if not control.isHidden())
            gap = 6 if card is self.sensor_card else 0
            width = max(100, card.width() - reserve - 48 - gap)
            card.titleLabel.setFixedWidth(width)
            card.contentLabel.setFixedWidth(width)
            height = card.titleLabel.heightForWidth(width)
            card.titleLabel.setFixedHeight(height)
            if not card.contentLabel.isHidden():
                content_height = card.contentLabel.heightForWidth(width)
                card.contentLabel.setFixedHeight(content_height)
                height += content_height + card.vBoxLayout.spacing()
            card.setFixedHeight(max(56, height + 26))
        for group in self.groups:
            group.adjustSize()

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
        QTimer.singleShot(0, self.fit_cards)

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
