"""Fluent desktop views over the shared UI ports; no device or service access."""
import math
import sys
import time

from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QIcon, QPainter, QPixmap, QFont, QColor, QRadialGradient
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QApplication, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QButtonGroup, QStackedWidget, QScroller, QScrollerProperties
from qfluentwidgets import (
    FluentWidget, FluentIcon as Icon, SegmentedWidget, SettingCardGroup, SwitchSettingCard, SettingCard,
    TitleLabel, BodyLabel, CaptionLabel,
    PrimaryPushButton, PushButton, RadioButton, ComboBox,
    ScrollArea, InfoBar, InfoBarPosition, MessageBox, Theme, setTheme,
    setThemeColor, isDarkTheme, qconfig, setCustomStyleSheet, setFont,
)

from .. import __version__
from ..frontend.actions import ActionRunner, SettingsModel
from ..frontend.ports import UIClient
from ..frontend.state import PostureModel, Detail
from .demo import DemoClient, UnavailableClient
from .resources import data_root, translator

WINDOW_LIGHT = '#f6f7f9'
WINDOW_DARK = '#22252b'
TEXT_LIGHT, TEXT_DARK = '#45494e', '#dcdee1'
MUTED_LIGHT, MUTED_DARK = '#656a70', '#b4b8be'


def mode_labels(_):
    return {'auto': _('Automatic'), 'laptop': _('Laptop'), 'tent': _('Tent'),
            'stand': _('Stand'), 'tablet': _('Tablet')}


class Illustration(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pose = 'laptop'
        self.setFixedHeight(216)
        qconfig.themeChanged.connect(self.update)

    def paintEvent(self, event):
        path = data_root() / 'assets' / f'{self.pose}.svg'
        dark = isDarkTheme()
        svg = path.read_text(encoding='utf-8').replace('currentColor', '#d6dfee' if dark else '#40516e')
        svg = svg.replace('var(--app-bg, white)', '#2b3445' if dark else '#eff4fc')
        renderer = QSvgRenderer(svg.encode())
        painter = QPainter(self)
        width = min(self.width(), self.height() * 4 / 3)
        bounds = QRectF(0, 0, width, width * 3 / 4)
        bounds.moveCenter(QRectF(self.rect()).center())
        glow = QRadialGradient(bounds.center(), bounds.height() / 2)
        glow.setColorAt(0, QColor(83, 135, 235, 38 if dark else 24))
        glow.setColorAt(1, Qt.GlobalColor.transparent)
        painter.fillRect(bounds, glow)
        renderer.render(painter, bounds)


class Page(ScrollArea):
    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.setObjectName(name)
        self.setWidgetResizable(True)
        # Register only touch dragging. Mouse clicks and the wheel retain their
        # normal behavior; the recognizer cancels a tap when it becomes a drag.
        QScroller.grabGesture(self.viewport(), QScroller.ScrollerGestureType.TouchGesture)
        properties = QScroller.scroller(self.viewport()).scrollerProperties()
        properties.setScrollMetric(QScrollerProperties.ScrollMetric.HorizontalOvershootPolicy,
                                   QScrollerProperties.OvershootPolicy.OvershootAlwaysOff)
        properties.setScrollMetric(QScrollerProperties.ScrollMetric.VerticalOvershootPolicy,
                                   QScrollerProperties.OvershootPolicy.OvershootAlwaysOff)
        QScroller.scroller(self.viewport()).setScrollerProperties(properties)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet('QScrollArea {background: transparent; border: none;}')
        content = QWidget()
        content.setObjectName(name + 'Content')
        content.setStyleSheet(f'#{name}Content {{background: transparent;}}')
        self.setWidget(content)
        self.box = QVBoxLayout(content)
        self.box.setContentsMargins(20, 16, 20, 16)
        self.box.setSpacing(14)



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
        self.confirm_button.hide()
        self.retry_button.hide()
        buttons = QHBoxLayout()
        buttons.addStretch()
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.retry_button)
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


# SettingCard paints a fixed 6 px outline on every row. Let Qt draw the
# grouped surfaces from the stylesheet, retaining the Fluent controls inside.
class SettingRow(SettingCard):
    def paintEvent(self, event):
        QFrame.paintEvent(self, event)


class SwitchRow(SwitchSettingCard):
    def paintEvent(self, event):
        QFrame.paintEvent(self, event)


def row_style(surface, line, primary, secondary):
    return f"""
        SettingCard {{ background: {surface}; border: 1px solid {line}; border-top: none; }}
        SettingCard[first="true"] {{
            border-top: 1px solid {line};
            border-top-left-radius: 16px; border-top-right-radius: 16px;
        }}
        SettingCard[last="true"] {{ border-bottom-left-radius: 16px; border-bottom-right-radius: 16px; }}
        QLabel#settingTitle {{ color: {primary}; font-size: 14px; font-weight: 400; }}
        QLabel#contentLabel {{ color: {secondary}; font-size: 12px; }}
    """


class PageSwitcher(SegmentedWidget):
    # Delegate the selected surface to the native item's isSelected stylesheet.
    # The stock parent's painter always draws a second, 5 px selection outline.
    def paintEvent(self, event):
        QWidget.paintEvent(self, event)

    def __init__(self, parent):
        super().__init__(parent)
        self.hBoxLayout.setContentsMargins(4, 4, 4, 4)
        self.hBoxLayout.setSpacing(2)
        setCustomStyleSheet(self,
            'PageSwitcher { background: #e9ecf1; border: none; border-radius: 14px; }',
            'PageSwitcher { background: #30343c; border: none; border-radius: 14px; }')

    def fit_items(self):
        for item in self.items.values():
            item.setFixedHeight(40)
            setCustomStyleSheet(item,
                self.item_style(MUTED_LIGHT, '#e0e5ed', '#ffffff', '#305b98', '#c7d8f7'),
                self.item_style(MUTED_DARK, '#393e48', '#414b5e', '#d3e2f8', '#566c91'))
        width = max(104, *(item.sizeHint().width() for item in self.items.values()))
        for item in self.items.values():
            item.setFixedWidth(width)
        self.setFixedSize(self.sizeHint())

    @staticmethod
    def item_style(text, hover, selected, accent, pressed):
        return f"""
            SegmentedItem[isSelected] {{
                color: {text}; border: 1px solid transparent; border-radius: 10px; margin: 0;
                padding-top: 0; padding-bottom: 0; font-size: 13px; font-weight: 400;
            }}
            SegmentedItem[isSelected=false]:hover {{ background: {hover}; }}
            SegmentedItem[isSelected=true], SegmentedItem[isSelected=true]:hover {{
                color: {accent}; background: {selected};
            }}
            SegmentedItem[isSelected]:pressed {{ background: {pressed}; }}
            SegmentedItem[isSelected]:focus {{ border-color: {accent}; }}
        """


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
            ('tent_enabled', _('Enable tent mode'), _('Allow automatic detection of tent mode')),
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
