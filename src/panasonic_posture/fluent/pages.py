"""Posture and settings pages over the shared frontend models."""
import math
import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QButtonGroup
from qfluentwidgets import (
    FluentIcon as Icon, SettingCardGroup, SettingCard,
    TitleLabel, BodyLabel, CaptionLabel,
    PrimaryPushButton, PushButton, ComboBox, MessageBox, HyperlinkButton,
    Theme, setTheme, isDarkTheme, qconfig, setCustomStyleSheet, setFont,
)

from .. import __version__
from ..build_info import display_version
from ..contracts import ALL_ERROR_CODES, MODES, ErrorCode
from ..frontend.actions import ActionRunner, SettingsModel
from ..frontend.ports import UIClient
from ..frontend.state import PostureModel, Detail
from .widgets import (
    WINDOW_LIGHT, WINDOW_DARK, TEXT_LIGHT, TEXT_DARK, MUTED_LIGHT, MUTED_DARK,
    Illustration, Page, ModeRadio, SettingRow, SwitchRow, row_style,
    apply_app_font, BODY_SIZE, SECTION_TITLE_SIZE, EMPHASIS_WEIGHT,
)
from .update_check import UpdateChecker


GITHUB_URL = 'https://github.com/adogecheems/panasonic-posture-manager'
APP_VERSION = display_version()


def mode_label(mode, _):
    if mode == 'auto':
        return _('Automatic')
    if mode == 'laptop':
        return _('Laptop')
    if mode == 'tent':
        return _('Tent')
    if mode == 'stand':
        return _('Stand')
    if mode == 'tablet':
        return _('Tablet')
    return _('Waiting for the desktop to get ready…')


def mode_labels(_):
    return {mode: mode_label(mode, _) for mode in
            ('auto', 'laptop', 'tent', 'stand', 'tablet')}


def set_visible(widget, visible: bool) -> None:
    if widget.isHidden() == visible:
        widget.setVisible(visible)


def set_enabled(widget, enabled: bool) -> None:
    if widget.isEnabled() != enabled:
        widget.setEnabled(enabled)


def set_text(widget, text: str) -> None:
    if widget.text() != text:
        widget.setText(text)


def stable_error_text(code: str | None, _) -> str:
    """Localize stable service failures without exposing diagnostic detail."""
    messages = {
        ErrorCode.INVALID_MODE: _('This mode is not available right now.'),
        ErrorCode.ACCESS_DENIED: _('Please switch back to the local desktop and try again.'),
        ErrorCode.UNSUPPORTED_MODEL: _('This model does not support automatic mode changes.'),
        ErrorCode.HARDWARE_PRIORITY: _('The tablet switch currently controls the mode.'),
        ErrorCode.EXPIRED_CONFIRMATION: _('The posture prompt has expired. Wait for it to appear again.'),
        ErrorCode.CONFIG_WRITE: _('The setting could not be saved. Please try again.'),
        ErrorCode.RELAY_START: _('The internal keyboard and touchpad could not be controlled. Please try again.'),
        ErrorCode.RELAY_EXITED: _('Internal input control stopped. Please try again.'),
        ErrorCode.RELAY_HEARTBEAT: _('Internal input control stopped responding. Please try again.'),
        ErrorCode.RELAY_INPUT: _('The internal keyboard and touchpad could not be disabled. Please try again.'),
        ErrorCode.NATIVE_RECOVERY: _('The tablet switch state could not be restored. Restart the service if input behaves unexpectedly.'),
        ErrorCode.SENSOR_TIMEOUT: _('No posture data is available right now.'),
        ErrorCode.CALIBRATION_NOT_READY: _('Place the device in laptop form on a flat desk and hold it still for about two seconds, then try again.'),
        ErrorCode.DISPLAY_MIRRORED: _('This screen arrangement cannot be rotated. Please adjust the display settings.'),
        ErrorCode.DISPLAY_LAYOUT: _('The external display arrangement does not allow this mode.'),
        ErrorCode.DISPLAY_TRANSFORM: _('The screen rotation could not be applied. Please try again.'),
        ErrorCode.DISPLAY_LOCK: _('The rotation lock could not be changed. Please try again.'),
        ErrorCode.DISPLAY_UNAVAILABLE: _('Screen rotation is not available right now.'),
        ErrorCode.DISPLAY_UNKNOWN_MODE: _('This display mode is not supported.'),
        ErrorCode.DISPLAY_IDENTITY: _('The internal screen changed while applying the mode. Please try again.'),
        ErrorCode.INTERNAL: _('Something went wrong. Please try again.'),
    }
    return messages.get(code, messages[ErrorCode.INTERNAL])


def action_error_code(error: BaseException) -> str | None:
    code = getattr(error, 'code', None)
    if ((not isinstance(code, str) or code not in ALL_ERROR_CODES) and error.args and
            isinstance(error.args[0], str) and error.args[0] in ALL_ERROR_CODES):
        code = error.args[0]
    return code if isinstance(code, str) and code in ALL_ERROR_CODES else None


def startup_failure_text(reason: str | None, _) -> str | None:
    messages = {
        'backend-startup-failed': _('The background service could not start. Please try again.'),
    }
    return messages.get(reason)


class Home(Page):
    def __init__(self, client: UIClient, _, notify, parent=None):
        super().__init__('posture', parent)
        self._, self.client, self.notify = _, client, notify
        self.model = PostureModel()
        self.labels = mode_labels(_)
        self.action_detail = None
        self.actions = ActionRunner(self.sync_buttons, self.action_failed, client.refresh)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.timeout.connect(self.show_slow)
        box = self.box
        box.setSpacing(4)
        box.addStretch()
        self.illustration = Illustration()
        self.illustration.setAccessibleName(self.labels['laptop'])
        box.addWidget(self.illustration)
        self.heading = TitleLabel(self.labels['laptop'])
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        box.addWidget(self.heading)
        self.detail = BodyLabel('')
        self.detail.setWordWrap(True)
        self.detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail.hide()
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
        self.action_detail = None
        old_pose = self.model.pose
        old_deadline = self.model.deadline
        self.model.update(state, time.monotonic())
        if old_deadline != self.model.deadline:
            self.timer.stop()
            if self.model.deadline is not None:
                self.timer.start(max(1, math.ceil((self.model.deadline - time.monotonic()) * 1000)))
        if self.model.pose != old_pose:
            self.illustration.pose = self.model.pose
            self.illustration.setAccessibleName(self.labels[self.model.pose])
            self.illustration.update()
            self.heading.setText(self.labels[self.model.pose])
        self.render_detail()
        set_visible(self.confirm_button, state['pending_notification'] is not None)
        set_visible(self.retry_button, bool(state['error_code']))
        set_visible(self.auto_button, self.model.can_return_auto)
        self.sync_buttons()

    def render_detail(self):
        _ = self._
        messages = {
            Detail.NONE: '', Detail.DISCONNECTED: _('The background service is not connected yet.'),
            Detail.SESSION: _('Waiting for the desktop to get ready…'),
            Detail.UNSUPPORTED: _('This model does not support automatic mode changes.'),
            Detail.SWITCH: _('Tablet switch state is unavailable. Restart the background service or check the tablet device configuration.'),
            Detail.SENSOR: _('No posture data is available. Keep the device still; switching will resume automatically.'),
            Detail.SLOW: _('Switching is taking longer than usual. Please wait or choose another mode in Settings.'),
            Detail.COMPATIBLE: '',
        }
        if self.action_detail is not None:
            text = self.action_detail
        elif self.model.detail == Detail.ERROR:
            code = self.model.state['error_code'] if self.model.state else None
            text = f"{_('Mode change failed')}: {stable_error_text(code, _)}"
        else:
            text = messages[self.model.detail]
        set_text(self.detail, text)
        set_visible(self.detail, bool(self.detail.text()))

    def show_slow(self):
        self.model.advance(time.monotonic())
        # A callback must not consume the deadline if the platform wakes early.
        if self.model.deadline is not None and not self.model.slow:
            self.timer.start(max(1, math.ceil((self.model.deadline - time.monotonic()) * 1000)))
        self.render_detail()

    def unavailable(self):
        self.timer.stop()
        self.actions.disconnect()
        self.action_detail = None
        self.model.disconnect()
        failure = startup_failure_text(
            getattr(self.client, 'startup_failure', None), self._
        )
        if failure is None:
            self.render_detail()
        else:
            self.detail.setText(failure)
            self.detail.setVisible(True)
        self.confirm_button.hide()
        self.retry_button.hide()
        self.auto_button.hide()

    def action_failed(self, error):
        _ = self._
        reason = stable_error_text(action_error_code(error), _)
        self.action_detail = (
            f"{_('Mode change failed')}: "
            f"{reason}"
        )
        self.render_detail()
        self.notify(False, reason)

    def sync_buttons(self):
        prompt = self.model.state and self.model.state['pending_prompt']
        set_text(self.confirm_button,
                 self._('Enter stand mode') if prompt == 'stand' else self._('Enter tent mode'))
        set_text(self.retry_button,
            self._('Retrying…') if self.actions.busy == 'retry' else self._('Retry')
        )
        set_enabled(self.confirm_button, self.actions.busy is None)
        set_enabled(self.retry_button, self.actions.busy is None)
        set_enabled(self.auto_button, self.actions.busy is None)

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
    def __init__(self, client: UIClient, _, notify, parent=None, *, startup=None,
                 update_checker=None):
        super().__init__('settings', parent)
        self._render_signature = None
        self._fit_scheduled = False
        self._, self.client, self.notify = _, client, notify
        self.model = SettingsModel(client, self.render, self.action_failed)
        self.calibration = ActionRunner(self.render_calibration, self.action_failed, self.calibrated)
        self.update_checker = update_checker or UpdateChecker(
            __version__, parent=self
        )
        self.update_checker.completed.connect(self.update_checked)
        self.update_checker.failed.connect(self.update_failed)
        self.update_checking = False
        self.switches, self.radios = {}, {}
        self.cards = []
        self.groups = []
        self.startup = startup
        self.startup_card = None
        self.box.setContentsMargins(24, 16, 24, 20)
        self.box.setSpacing(18)
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
        self.calibrate_button = PrimaryPushButton(_('Calibrate'))
        self.calibrate_button.clicked.connect(self.ask_calibration)
        self.reset_button = PushButton(_('Reset'))
        self.reset_button.clicked.connect(lambda: self.calibration.run('reset', self.client.reset_calibration))
        self.sensor_card.hBoxLayout.addWidget(
            self.reset_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self.sensor_card.hBoxLayout.addSpacing(6)
        self.sensor_card.hBoxLayout.addWidget(
            self.calibrate_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self.sensor_card.hBoxLayout.addSpacing(16)
        self.add_card(group, self.sensor_card, (self.reset_button, self.calibrate_button))
        group = self.add_group(_('Mode selection'))
        self.group = QButtonGroup(self)
        descriptions = {
            'auto': _('Switch automatically with the current posture'),
            'laptop': _('Best for typing and precise work'),
            'tent': _('Designed for use in tent form'),
            'stand': _('Designed for viewing in stand form'),
        }
        self.mode_cards = {}
        self.mode_descriptions = descriptions
        labels = mode_labels(_)
        for key in MODES:
            label = labels[key]
            card = SettingRow(QIcon(), label, descriptions[key])
            self.mode_cards[key] = card
            radio = ModeRadio()
            radio.setAccessibleName(label)
            self.group.addButton(radio)
            radio.clicked.connect(lambda checked, mode=key: self.model.queue('manual', mode))
            self.radios[key] = radio
            card.hBoxLayout.insertWidget(0, radio)
            card.hBoxLayout.setContentsMargins(16, 0, 0, 0)
            self.add_card(group, card, (radio,))
        if startup is not None:
            group = self.add_group(_('Background'))
            self.startup_card = SwitchRow(QIcon(), _('Start at login'), '')
            self.startup_switch = self.startup_card.switchButton
            self.startup_switch.label.hide()
            self.startup_switch.hBox.setContentsMargins(0, 0, 0, 0)
            self.startup_switch.setFixedWidth(self.startup_switch.indicator.width())
            self.startup_switch.setAccessibleName(_('Start at login'))
            self.startup_card.checkedChanged.connect(self.change_startup)
            self.add_card(group, self.startup_card, (self.startup_switch,))
            self.refresh_startup()
        group = self.add_group(_('About'))
        self.version_card = SettingRow(QIcon(), _('Version'), APP_VERSION)
        self.update_button = PrimaryPushButton(Icon.SYNC, _('Check for updates'))
        self.update_button.clicked.connect(self.check_updates)
        self.version_card.hBoxLayout.addWidget(
            self.update_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self.version_card.hBoxLayout.addSpacing(16)
        self.add_card(group, self.version_card, (self.update_button,))
        self.add_card(group, SettingRow(QIcon(), _('Author'), 'adogecheems'))
        self.github_button = HyperlinkButton(GITHUB_URL, _('Visit repository'))
        self.github_button.setAccessibleName(_('Visit repository'))
        github = SettingRow(
            QIcon(), _('GitHub'), 'github.com/adogecheems/panasonic-posture-manager'
        )
        github.hBoxLayout.addWidget(
            self.github_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        github.hBoxLayout.addSpacing(16)
        self.github_card = github
        self.add_card(group, github, (self.github_button,))
        appearance = ComboBox()
        for text in (_('System'), _('Light'), _('Dark')):
            appearance.addItem(text)
        appearance.setAccessibleName(_('Appearance'))
        appearance.currentIndexChanged.connect(lambda index: setTheme((Theme.AUTO, Theme.LIGHT, Theme.DARK)[index]))
        card = SettingRow(QIcon(), _('Appearance'))
        card.hBoxLayout.addWidget(appearance, 0, Qt.AlignmentFlag.AlignVCenter)
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
        setFont(group.titleLabel, SECTION_TITLE_SIZE, EMPHASIS_WEIGHT)
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
        self._schedule_fit()

    def tint_scroll_edge(self):
        color = WINDOW_DARK if isDarkTheme() else WINDOW_LIGHT
        self.scroll_edge.setStyleSheet(
            'border: none; background: qlineargradient(x1:0, y1:0, x2:0, y2:1, '
            f'stop:0 transparent, stop:1 {color});')

    def update_scroll_edge(self):
        bar = self.verticalScrollBar()
        self.scroll_edge.setVisible(bar.value() < bar.maximum())

    def _schedule_fit(self):
        if self._fit_scheduled:
            return
        self._fit_scheduled = True
        QTimer.singleShot(0, self.fit_cards)

    def fit_cards(self):
        self._fit_scheduled = False
        for card, controls in self.cards:
            reserve = sum(control.sizeHint().width() for control in controls if not control.isHidden())
            gap = 6 if card is self.sensor_card else 0
            width = max(100, card.width() - reserve - 48 - gap)
            card.titleLabel.setFixedWidth(width)
            card.contentLabel.setFixedWidth(width)
            # QLabel includes its previous minimum height when measuring wrapped text.
            card.titleLabel.setMinimumHeight(0)
            height = card.titleLabel.heightForWidth(width)
            card.titleLabel.setFixedHeight(height)
            if not card.contentLabel.isHidden():
                card.contentLabel.setMinimumHeight(0)
                content_height = card.contentLabel.heightForWidth(width)
                card.contentLabel.setFixedHeight(content_height)
                height += content_height + card.vBoxLayout.spacing()
            card.setFixedHeight(max(52, height + 20))
        for group in self.groups:
            group.adjustSize()

    def update_state(self, state):
        self.model.update(state)
        self.render()

    def check_updates(self):
        if self.update_checking:
            return
        self.update_checking = True
        self.update_button.setText(self._('Checking…'))
        self.update_button.setEnabled(False)
        if not self.update_checker.check() and self.update_checking:
            self.update_failed()

    def update_checked(self, info):
        self.update_checking = False
        status = (self._('New version available: {version}').format(version=info.latest)
                  if info.update_available else self._('Up to date'))
        self.version_card.setContent(f'{APP_VERSION} · {status}')
        self.update_button.setText(self._('Check for updates'))
        self.update_button.setEnabled(True)
        self._schedule_fit()

    def update_failed(self):
        self.update_checking = False
        status = self._('Unable to check for updates. Try again later.')
        self.version_card.setContent(f'{APP_VERSION} · {status}')
        self.update_button.setText(self._('Check for updates'))
        self.update_button.setEnabled(True)
        self._schedule_fit()

    def shutdown(self):
        self.update_checker.shutdown()

    def refresh_startup(self):
        if self.startup_card is None:
            return
        _ = self._
        try:
            status = self.startup.status()
            enabled, available, conflict = status.enabled, status.available, status.conflict
        except (OSError, ValueError):
            enabled = available = conflict = False
        self.startup_switch.blockSignals(True)
        self.startup_switch.setChecked(enabled)
        self.startup_switch.setEnabled(available)
        self.startup_switch.blockSignals(False)
        text = (_('A login startup entry already points to another app or location.') if conflict else
                _('Login startup is unavailable from this location.') if not available else
                _('Start in the system tray when you sign in.'))
        self.startup_card.setContent(text)
        self._schedule_fit()

    def change_startup(self, enabled):
        try:
            self.startup.set_enabled(enabled)
        except (OSError, ValueError):
            self.notify(False)
        else:
            self.notify(True)
        finally:
            self.refresh_startup()

    def render(self):
        _ = self._
        if self.model.state is None:
            return
        signature = self.model.render_signature()
        if signature == self._render_signature:
            return
        self._render_signature = signature
        for key, radio in self.radios.items():
            checked = self.model.value('manual') == key
            if radio.isChecked() != checked:
                radio.setChecked(checked)
            set_enabled(radio, self.model.mode_enabled(key))
        for field, switch in self.switches.items():
            checked = self.model.value(field)
            if switch.isChecked() != checked:
                switch.blockSignals(True)
                switch.setChecked(checked)
                switch.blockSignals(False)
            set_enabled(switch, self.model.preference_enabled(field))
        self.render_calibration()
        self._schedule_fit()

    def render_calibration(self):
        state = self.model.state
        enabled = bool(state and state['supported'] and self.calibration.busy is None)
        set_enabled(self.calibrate_button, enabled and state['hardware'] is not True)
        set_enabled(self.reset_button, enabled)
        calibrated = bool(state and state['sensor_calibrated'])
        set_visible(self.reset_button, calibrated)
        set_text(self.calibration_hint,
                 self._('A custom flat-desk reference is in use.') if calibrated else
                 self._('Place the device in laptop form on a flat desk, then calibrate.'))

    def calibrated(self):
        self.notify(True)
        self.client.refresh()

    def action_failed(self, error):
        self.notify(False, stable_error_text(action_error_code(error), self._))

    def ask_calibration(self):
        _ = self._
        connection = self.model.connection
        dialog = MessageBox(_('Calibrate sensor?'),
                            _('Place the device in laptop form on a flat, stable desk and wait about two seconds before confirming. Its current direction will become the level reference.'), self.window())
        dialog.yesButton.setText(_('Calibrate'))
        dialog.cancelButton.setText(_('Cancel'))
        apply_app_font(dialog.titleLabel, SECTION_TITLE_SIZE, EMPHASIS_WEIGHT)
        apply_app_font(dialog.contentLabel, BODY_SIZE)
        if dialog.exec() and connection == self.model.connection:
            self.calibration.run('calibrate', self.client.calibrate)

    def unavailable(self):
        self.model.disconnect()
        self.calibration.disconnect()
        self._render_signature = None
        for widget in (*self.radios.values(), *self.switches.values(), self.calibrate_button, self.reset_button):
            widget.setEnabled(False)
