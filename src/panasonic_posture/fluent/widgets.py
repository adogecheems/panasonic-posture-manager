"""Shared Fluent controls and their existing appearance and touch behavior."""
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QColor, QPainter, QRadialGradient
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QScroller, QScrollerProperties,
)
from qfluentwidgets import (
    RadioButton, ScrollArea, SegmentedWidget, SettingCard, SwitchSettingCard,
    isDarkTheme, qconfig, setCustomStyleSheet,
)

from .resources import data_root

WINDOW_LIGHT = '#f6f7f9'
WINDOW_DARK = '#22252b'
TEXT_LIGHT, TEXT_DARK = '#45494e', '#dcdee1'
MUTED_LIGHT, MUTED_DARK = '#656a70', '#b4b8be'


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
