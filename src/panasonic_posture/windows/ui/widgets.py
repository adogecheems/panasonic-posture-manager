"""Shared Fluent controls and their existing appearance and touch behavior."""
from PyQt6.QtCore import Qt, QRectF, QSize
from PyQt6.QtGui import QColor, QFont, QPainter, QRadialGradient
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QScroller, QScrollerProperties,
)
from qfluentwidgets import (
    RadioButton, ScrollArea, SegmentedWidget, SettingCard, SwitchSettingCard,
    fontStyleSheet, getFont, isDarkTheme, qconfig, setCustomStyleSheet, setFont,
    setStyleSheet, CustomStyleSheet, StyleSheetBase,
    InfoBarIcon, drawIcon, themeColor,
)
from qfluentwidgets.common.style_sheet import addStyleSheet

from ..app.resources import data_root

WINDOW_LIGHT = '#f6f7f9'
WINDOW_DARK = '#22252b'
TEXT_LIGHT, TEXT_DARK = '#45494e', '#dcdee1'
MUTED_LIGHT, MUTED_DARK = '#656a70', '#b4b8be'

# Single source for the application's text roles. Transient surfaces such as
# modal dialogs and info banners reuse the same sizes and emphasis weights as
# the permanent pages instead of the Fluent library defaults.
HEADING_SIZE = 24
BODY_SIZE = 13
CAPTION_SIZE = 12
SECTION_TITLE_SIZE = 13
DIALOG_TITLE_SIZE = 14
DIALOG_BODY_SIZE = 12
EMPHASIS_WEIGHT = QFont.Weight.Medium


def apply_app_font(widget, size, weight=QFont.Weight.Normal,
                   light_color=None, dark_color=None):
    """Pin a widget to an application text role ahead of Fluent QSS rules.

    Fluent dialog and banner stylesheets declare ``font:`` sizes that outrank
    ``setFont`` in the style cascade. Setting the same rule on the widget
    itself keeps the application typography while themed colors and other
    declarations continue to arrive from the inherited sheets. When both
    colors are given the rule is registered as a themed custom style sheet so
    transient surfaces also follow theme switches.
    """
    setFont(widget, size, weight)
    rule = fontStyleSheet(getFont(size, weight))
    if weight != QFont.Weight.Normal:
        rule += f'; font-weight: {int(weight)}'
    if light_color is not None and dark_color is not None:
        setCustomStyleSheet(widget, f'{rule}; color: {light_color};',
                            f'{rule}; color: {dark_color};')
        # setCustomStyleSheet() only stores the themed rules. Registering the
        # widget with the style manager applies them now and on theme changes;
        # transient surfaces are created after the manager exists, so they
        # never receive the fluent sheets that would register them implicitly.
        setStyleSheet(widget, CustomStyleSheet(widget))
    else:
        widget.setStyleSheet(rule)


class NoticeIcon(QWidget):
    """Compact success/error glyph for the slim in-app notice bars."""

    def __init__(self, icon, parent=None):
        super().__init__(parent)
        self.icon = icon

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(2, 2, -2, -2)
        if self.icon == InfoBarIcon.INFORMATION:
            drawIcon(self.icon, painter, rect, indexes=[0],
                     fill=themeColor().name())
        else:
            drawIcon(self.icon, painter, rect)


def restyle_notice_bar(bar):
    """Slim the stock banner and center its text on the 24 px glyph."""
    icon = NoticeIcon(bar.iconWidget.icon, bar)
    if bar.hBoxLayout.replaceWidget(bar.iconWidget, icon) is None:
        raise RuntimeError('notice icon replacement failed')
    bar.iconWidget.deleteLater()
    bar.iconWidget = icon
    # The banner is already visible when the stock factory returns it, so the
    # swapped-in tile is not shown by the layout and would stay unmanaged.
    icon.show()
    icon.setFixedSize(24, 24)
    bar.closeButton.setFixedSize(28, 28)
    bar.closeButton.setIconSize(QSize(12, 12))
    bar.hBoxLayout.setContentsMargins(10, 4, 10, 4)
    # The stock banner glues the copy to the glyph and stretches the title
    # label across the bar, leaving a lopsided empty area before the close
    # button. A small fixed gap and natural-size labels keep both sides even.
    bar.hBoxLayout.insertSpacing(1, 8)
    bar.hBoxLayout.setAlignment(
        icon, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
    bar.hBoxLayout.setAlignment(bar.closeButton, Qt.AlignmentFlag.AlignVCenter)
    bar.textLayout.setContentsMargins(0, 0, 0, 0)
    bar.textLayout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
    bar.textLayout.setAlignment(bar.titleLabel, Qt.AlignmentFlag.AlignVCenter)
    bar.textLayout.setAlignment(bar.contentLabel, Qt.AlignmentFlag.AlignVCenter)
    bar.textLayout.setStretchFactor(bar.titleLabel, 0)
    bar.textLayout.setStretchFactor(bar.contentLabel, 0)
    if bar.contentLabel.isHidden():
        # The stock separator keeps reserving its 7 px even when the detail
        # is empty; without it the success copy sits level with the buttons.
        separator = bar.textLayout.itemAt(1)
        if (separator is not None and separator.widget() is None and
                separator.layout() is None):
            bar.textLayout.removeItem(separator)
    # showEvent() already measured the bar around the stock 36 px tiles;
    # re-measure so the frame tracks the compact geometry.
    bar.adjustSize()


class _FixedRule(StyleSheetBase):
    """Static QSS source that the style manager reapplies on theme changes."""

    def __init__(self, rule):
        super().__init__()
        self._rule = rule

    def path(self, theme=None):
        return ''

    def content(self, theme=None):
        return self._rule


def style_message_box(dialog):
    """Compact the stock message box insets to match the settings rows.

    The stock frame reserves a fixed 105 px allowance below the content for
    the 81 px button band plus a 24 px gap. Rebuild the band as an 8 px gap
    over a 68 px group, then restore the frame from the layout minimum so the
    bottom inset follows the real margins instead of the hard-coded number.
    The title keeps the application primary color and the detail follows the
    muted settings-row role through the Fluent theme mechanism.
    """
    apply_app_font(dialog.titleLabel, DIALOG_TITLE_SIZE,
                   light_color=TEXT_LIGHT, dark_color=TEXT_DARK)
    apply_app_font(dialog.contentLabel, DIALOG_BODY_SIZE)
    dialog.contentLabel.setTextColor(MUTED_LIGHT, MUTED_DARK)
    # BodyLabel repaints its own color sheet whenever the theme changes,
    # which replaces the widget style sheet. Append the size rule as a
    # stable source so the app size survives those repaints and outranks
    # the stock 14 px rule from the fluent dialog sheet.
    addStyleSheet(dialog.contentLabel, _FixedRule(
        f'QLabel#contentLabel {{ {fontStyleSheet(getFont(DIALOG_BODY_SIZE))}; }}'))
    dialog.textLayout.setContentsMargins(24, 24, 24, 16)
    dialog.buttonGroup.setFixedHeight(68)
    dialog.buttonLayout.setContentsMargins(24, 16, 24, 16)
    dialog.widget.setMinimumSize(0, 0)
    dialog.widget.setMaximumSize(16777215, 16777215)
    dialog.widget.adjustSize()


class Illustration(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.pose = 'laptop'
        self._renderers = {}
        self.setFixedHeight(200)
        qconfig.themeChanged.connect(self.update)

    def _renderer(self, path, dark):
        key = str(path), dark
        renderer = self._renderers.get(key)
        if renderer is None:
            svg = path.read_text(encoding='utf-8').replace(
                'currentColor', '#d6dfee' if dark else '#40516e')
            svg = svg.replace('var(--app-bg, white)',
                              '#2b3445' if dark else '#eff4fc')
            renderer = QSvgRenderer(svg.encode())
            self._renderers[key] = renderer
        return renderer

    def paintEvent(self, event):
        path = data_root() / 'assets' / f'{self.pose}.svg'
        dark = isDarkTheme()
        renderer = self._renderer(path, dark)
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
