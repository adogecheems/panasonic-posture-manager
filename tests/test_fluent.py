"""Windows UI preview: pure demo state plus optional isolated Qt widgets."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest

from panasonic_posture.fluent.demo import DemoClient, UnavailableClient


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.jobs, self.states = [], []
        self.client = DemoClient(lambda delay, callback: self.jobs.append(callback))
        self.client.start(self.states.append, lambda: None)

    def drain(self):
        while self.jobs:
            self.jobs.pop(0)()

    def test_mode_request_and_completion_are_distinct(self):
        future = self.client.set_mode('tent')
        self.assertTrue(future.done())
        self.assertIsNone(self.states[-1]['applied'])
        self.assertEqual(self.states[-1]['target']['mode'], 'tent')
        self.drain()
        self.assertEqual(self.states[-1]['applied']['mode'], 'tent')

    def test_hardware_and_disabled_tent_reject_requests(self):
        self.client.scenario('hardware')
        self.assertIsNotNone(self.client.set_mode('laptop').exception())
        self.client.scenario('laptop')
        self.client.set_preference('tent_enabled', False)
        self.assertIsNotNone(self.client.set_mode('tent').exception())

    def test_stale_completion_and_stop_do_not_publish(self):
        self.client.set_mode('tent')
        self.client.scenario('hardware')
        self.drain()
        self.assertEqual(self.states[-1]['applied']['mode'], 'hardware')
        self.client.scenario('laptop')
        self.client.set_mode('stand')
        self.client.stop()
        count = len(self.states)
        self.drain()
        self.assertEqual(len(self.states), count)

    def test_prompt_epoch_and_calibration(self):
        self.client.scenario('prompt')
        self.assertIsNotNone(self.client.confirm_prompt('tent', 0).exception())
        epoch = self.states[-1]['pending_notification']
        self.client.confirm_prompt('tent', epoch)
        self.drain()
        self.assertEqual(self.states[-1]['applied']['mode'], 'tent')
        self.client.calibrate()
        self.assertTrue(self.states[-1]['sensor_calibrated'])
        self.client.reset_calibration()
        self.assertFalse(self.states[-1]['sensor_calibrated'])

    def test_unavailable_client_never_reports_success(self):
        client = UnavailableClient()
        unavailable = []
        client.start(lambda value: self.fail('unexpected state'), lambda: unavailable.append(True))
        self.assertEqual(unavailable, [True])
        self.assertIsNotNone(client.set_mode('tent').exception())

    def test_cli_help_does_not_load_qt_or_gi(self):
        result = subprocess.run([sys.executable, '-S', '-m', 'panasonic_posture.fluent', '--help'],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--demo', result.stdout)

    def test_scenario_requires_explicit_demo(self):
        result = subprocess.run([sys.executable, '-S', '-m', 'panasonic_posture.fluent',
                                 '--scenario', 'hardware'], text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--scenario requires --demo', result.stderr)

    def test_source_catalog_fallback(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from panasonic_posture.fluent.resources import translator
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'po').mkdir()
            (root / 'po/zh_CN.po').write_text('msgid "Laptop"\nmsgstr "笔记本"\n', encoding='utf-8')
            with patch.dict(os.environ, PANASONIC_POSTURE_DATA_DIR=str(root / 'data')):
                self.assertEqual(translator('zh_CN')('Laptop'), '笔记本')
                self.assertEqual(translator('en')('Laptop'), 'Laptop')


@unittest.skipUnless(importlib.util.find_spec('PyQt6') and importlib.util.find_spec('qfluentwidgets'),
                     'optional Fluent preview dependencies are not installed')
class FluentWidgetTests(unittest.TestCase):
    def run_ui(self, body, scale='1'):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR=scale,
                   LANGUAGE='en', LANG='C.UTF-8')
        root = Path(__file__).resolve().parents[1]
        env['PYTHONPATH'] = str(root / 'src')
        prelude = '''
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == 'gi' or name.endswith('linux_contract'):
            raise AssertionError(name)
sys.meta_path.insert(0, Block())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtTest import QTest
from panasonic_posture.fluent.app import Window
from panasonic_posture.fluent.demo import DemoClient, UnavailableClient
app = QApplication([])
window = Window(demo=True, language='en')
window.show()
QTest.qWait(100)
'''
        cleanup = '''
from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6 import sip
if not sip.isdeleted(window):
    window.close()
QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
assert sip.isdeleted(window), 'Closed windows must be destroyed before interpreter shutdown'
'''
        result = subprocess.run([sys.executable, '-c', prelude + body + cleanup],
                                env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_event_loop_destroys_closed_window(self):
        self.run_ui('''
from PyQt6.QtCore import QTimer
from PyQt6 import sip
app.setQuitOnLastWindowClosed(True)
QTimer.singleShot(0, window.close)
assert app.exec() == 0
assert sip.isdeleted(window)
assert window.client._stopped
''')

    def test_mode_settings_and_calibration(self):
        self.run_ui('''
window.settings.radios['tent'].click()
QTest.qWait(450)
assert window.home.heading.text() == 'Tent'
window.settings.switches['tent_enabled'].setChecked(False)
assert not window.settings.radios['tent'].isEnabled()
window.settings.calibration.run('calibrate', window.client.calibrate)
assert window.settings.reset_button.isVisibleTo(window.settings)
''')

    def test_scenarios_disconnect_and_restore(self):
        self.run_ui('''
window.client.scenario('hardware')
assert not any(button.isEnabled() for button in window.settings.radios.values())
assert window.home.heading.text() == 'Tablet'
window.client.scenario('prompt')
assert window.home.confirm_button.isVisibleTo(window.home)
window.home.confirm_button.click()
QTest.qWait(450)
assert window.home.heading.text() == 'Tent'
window.client.scenario('disconnected')
assert not window.settings.calibrate_button.isEnabled()
window.client.scenario('laptop')
assert window.settings.radios['laptop'].isEnabled()
''')

    def test_normal_start_has_no_simulated_connection(self):
        self.run_ui('''
normal = Window(language='en')
assert isinstance(normal.client, UnavailableClient)
assert not normal.settings.radios['tent'].isEnabled()
assert 'not connected' in normal.home.detail.text().lower()
normal.close()
''')

    def test_keyboard_browsing_and_late_confirmation(self):
        self.run_ui('''
from PyQt6.QtCore import Qt
window.switchTo(window.settings)
radio = window.settings.radios['auto']
radio.setFocus()
QTest.keyClick(radio, Qt.Key.Key_Down)
assert window.client.state['manual'] == 'auto'
assert window.settings.radios['auto'].isChecked()
window.client.scenario('slow')
QTest.qWait(2100)
assert 'longer than usual' in window.home.detail.text()
window.client.scenario('disconnected')
assert not window.home.timer.isActive()
''')

    def test_chinese_dark_and_minimum_layout(self):
        self.run_ui('''
from qfluentwidgets import setTheme, Theme
translated = Window(demo=True, language='zh_CN')
translated.resize(500, 660)
translated.show()
setTheme(Theme.DARK)
QTest.qWait(100)
assert translated.home.heading.text() == '笔记本'
assert translated.home.horizontalScrollBar().maximum() == 0
translated.switchTo(translated.settings)
QTest.qWait(100)
assert translated.settings.horizontalScrollBar().maximum() == 0
assert translated.grab().width() == 500
translated.resize(420, 560)
QTest.qWait(150)
for card, controls in translated.settings.cards:
    if not card.contentLabel.isHidden():
        assert card.contentLabel.height() >= card.contentLabel.heightForWidth(card.contentLabel.width())
translated.close()
''')

    def test_compact_shell_logo_and_two_page_navigation(self):
        self.run_ui('''
from qfluentwidgets import setTheme, Theme
from PyQt6.QtGui import QIcon
from panasonic_posture.fluent.resources import data_root
assert (window.width(), window.height()) == (500, 660)
assert window.stackedWidget.count() == 2
assert not hasattr(window, 'navigationInterface')
assert window.stackedWidget.currentWidget() is window.home
window.navigation.items['settings'].click()
assert window.stackedWidget.currentWidget() is window.settings
assert window.navigation.currentRouteKey() == 'settings'
for theme, filename in ((Theme.LIGHT, 'lets-note-window-icon.png'),
                        (Theme.DARK, 'lets-note-window-icon-dark.png')):
    setTheme(theme)
    QTest.qWait(50)
    expected = QIcon(str(data_root() / 'assets' / filename)).pixmap(72, 42).toImage()
    assert window.windowIcon().pixmap(72, 42).toImage() == expected
    mark = window.titleBar.iconLabel.pixmap()
    assert mark.width() >= 72 * window.devicePixelRatioF()
    assert abs(mark.deviceIndependentSize().width() - 72) < 0.1
    assert mark.deviceIndependentSize().height() > 35
window.switchTo(window.home)
assert window.home.verticalScrollBar().maximum() == 0
''')

    def test_touch_drag_scrolls_without_changing_mode(self):
        self.run_ui('''
from PyQt6.QtWidgets import QScroller
from PyQt6.QtCore import QPointF, Qt
window.switchTo(window.settings)
QTest.qWait(100)
scroller = QScroller.scroller(window.settings.viewport())
assert QScroller.grabbedGesture(window.settings.viewport()) != 0
scroller.handleInput(QScroller.Input.InputPress, QPointF(200, 300), 0)
scroller.handleInput(QScroller.Input.InputMove, QPointF(200, 280), 40)
scroller.handleInput(QScroller.Input.InputMove, QPointF(200, 100), 140)
scroller.handleInput(QScroller.Input.InputRelease, QPointF(200, 100), 160)
QTest.qWait(200)
assert window.settings.verticalScrollBar().value() > 0
assert window.client.state['manual'] == 'auto'
assert window.client.state['tent_enabled'] is True
scroller.stop()
window.settings.verticalScrollBar().setValue(0)
QTest.mouseClick(window.settings.switches['tent_enabled'].indicator, Qt.MouseButton.LeftButton)
assert window.client.state['tent_enabled'] is False
''')

    def test_logo_retains_resolution_on_scaled_screens(self):
        for scale in ('1.5', '2'):
            with self.subTest(scale=scale):
                self.run_ui('''
mark = window.titleBar.iconLabel.pixmap()
assert mark.devicePixelRatio() == window.devicePixelRatioF()
assert mark.width() >= round(72 * window.devicePixelRatioF())
assert abs(mark.deviceIndependentSize().width() - 72) < 0.1
''', scale=scale)

    def test_native_controls_and_compact_navigation(self):
        self.run_ui('''
from qfluentwidgets import SegmentedWidget, SwitchSettingCard, SettingCardGroup
from PyQt6.QtGui import QFont, QPalette
assert isinstance(window.navigation, SegmentedWidget)
assert window.navigation.width() <= window.width() * 0.7
assert abs(window.navigation.geometry().center().x() - window.width() / 2) <= 2
assert len(window.settings.findChildren(SwitchSettingCard)) == 3
assert len(window.settings.findChildren(SettingCardGroup)) == 4
for card in window.settings.findChildren(SwitchSettingCard):
    assert card.titleLabel.font().weight() == QFont.Weight.Normal
    assert card.titleLabel.palette().color(QPalette.ColorRole.WindowText).name() != '#000000'
window.switchTo(window.settings)
window.resize(420, 560)
QTest.qWait(150)
for card, controls in window.settings.cards:
    if not card.contentLabel.isHidden():
        assert card.contentLabel.height() >= card.contentLabel.heightForWidth(card.contentLabel.width())
''')

    def test_grouped_rows_and_navigation_insets(self):
        self.run_ui('''
from PyQt6.QtCore import QPoint
window.switchTo(window.settings)
QTest.qWait(200)
first, middle, last = window.settings.switch_cards.values()
assert first.geometry().bottom() + 1 == middle.y()
assert middle.geometry().bottom() + 1 == last.y()
for card in (first, middle, last):
    switch = card.switchButton
    assert switch.rect().contains(switch.indicator.geometry())
nav = window.navigation
left, right = nav.items.values()
assert left.width() == right.width()
assert left.height() >= 40
assert left.x() == nav.width() - right.geometry().right() - 1 >= 4
assert left.y() == nav.height() - left.geometry().bottom() - 1 >= 4
# Check the rendered arc in the corner of a group, and the straight join below.
origin = first.mapTo(window, QPoint())
x, y = origin.x(), origin.y()
image = window.grab().toImage()
background = image.pixelColor(x - 2, y + 8)
assert image.pixelColor(x + 3, y + 3) == background
assert image.pixelColor(x + 3, y + first.height() - 3) != background
assert window.settings.scroll_edge.isVisible()
bar = window.settings.verticalScrollBar()
bar.setValue(bar.maximum())
assert not window.settings.scroll_edge.isVisible()
''')

    def test_navigation_contour_and_text_tones_in_both_themes(self):
        self.run_ui('''
from qfluentwidgets import Theme, setTheme
from PyQt6.QtCore import QPoint
window.switchTo(window.settings)
card = window.settings.sensor_card
for theme in (Theme.LIGHT, Theme.DARK):
    setTheme(theme)
    QTest.qWait(100)
    image = window.grab().toImage()
    # Sample the composited edge, including the background outside the curve.
    for widget, sample in ((window.navigation, QPoint(10, 1)),
                           (window.navigation.currentItem(), QPoint(8, 2))):
        origin = widget.mapTo(window, QPoint())
        middle = QPoint(widget.width() // 2, sample.y())
        assert image.pixelColor(origin + sample) == image.pixelColor(origin + middle)
        assert image.pixelColor(origin) != image.pixelColor(origin + middle)
    primary = card.titleLabel.palette().windowText().color()
    secondary = card.contentLabel.palette().windowText().color()
    assert primary != secondary
    assert window.settings.calibrate_button.palette().buttonText().color() == primary
    assert window.titleBar.titleLabel.palette().windowText().color() == secondary
    assert window.settings.groups[0].titleLabel.palette().windowText().color() == secondary
''')
