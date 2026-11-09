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

    def test_hardware_rejects_and_manual_tent_ignores_automatic_preference(self):
        self.client.scenario('hardware')
        self.assertIsNotNone(self.client.set_mode('laptop').exception())
        self.client.scenario('laptop')
        self.client.set_preference('tent_enabled', False)
        self.assertIsNone(self.client.set_mode('tent').exception())
        self.client.set_preference('tent_enabled', False)
        self.assertEqual(self.states[-1]['manual'], 'tent')

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

    def test_native_hardware_cli_rejects_software_options_before_loading_qt(self):
        for arguments in (['--demo'], ['--simulate-backend'], ['--settings', 'settings.json']):
            result = subprocess.run([sys.executable, '-S', '-m', 'panasonic_posture.fluent',
                '--hardware-backend', *arguments], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('cannot be combined', result.stderr)

    def test_native_screen_cli_rejects_preview_options_before_loading_qt(self):
        for arguments in (['--demo'], ['--backend'], ['--simulate-backend'], ['--hardware-backend']):
            result = subprocess.run([sys.executable, '-S', '-m', 'panasonic_posture.fluent',
                '--native-backend', *arguments], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('cannot be combined', result.stderr)

    def test_background_requires_native_before_loading_qt(self):
        result = subprocess.run([sys.executable, '-S', '-m', 'panasonic_posture.fluent', '--background'],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--background requires --native-backend', result.stderr)

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

    def test_new_source_strings_survive_an_older_compiled_catalog(self):
        import gettext
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from panasonic_posture.fluent.resources import translator
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'po').mkdir()
            (root / 'po/zh_CN.po').write_text('msgid "Stand"\nmsgstr "支架"\n', encoding='utf-8')
            compiled = gettext.GNUTranslations()
            compiled._catalog = {'Laptop': '笔记本'}
            compiled.plural = lambda n: int(n != 1)
            with patch.dict(os.environ, PANASONIC_POSTURE_DATA_DIR=str(root / 'data')):
                with patch('gettext.translation', return_value=compiled):
                    self.assertEqual(translator('zh_CN')('Laptop'), '笔记本')
                    self.assertEqual(translator('zh_CN')('Stand'), '支架')


@unittest.skipUnless(importlib.util.find_spec('PyQt6') and importlib.util.find_spec('qfluentwidgets'),
                     'optional Fluent preview dependencies are not installed')
class FluentWidgetTests(unittest.TestCase):
    def run_resident(self, body):
        self.run_ui('''
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtWidgets import QSystemTrayIcon
from panasonic_posture.fluent.resident import ResidentTray
class FakeTray(QObject):
    available = True
    activated = pyqtSignal(object)
    messageClicked = pyqtSignal()
    def __init__(self, icon, parent):
        super().__init__(parent)
        self.visible = False
        self.messages = []
    @classmethod
    def isSystemTrayAvailable(cls): return cls.available
    @staticmethod
    def supportsMessages(): return True
    def setContextMenu(self, menu): self.menu = menu
    def setToolTip(self, text): self.tooltip = text
    def show(self): self.visible = True
    def hide(self): self.visible = False
    def showMessage(self, *args): self.messages.append(args)
window.close()
client = DemoClient(QTimer.singleShot)
client.state.update(native_backend=True, input_control_available=False,
                    available_modes=['auto', 'laptop', 'tent', 'stand'])
window = Window(native_backend=True, client=client, language='en')
window.show()
''' + body)

    def test_resident_close_retains_backend_reopen_and_explicit_quit_stops_it(self):
        self.run_resident('''
from PyQt6 import sip
resident = ResidentTray(window, tray_factory=FakeTray)
assert resident.enabled and resident.tray.visible
assert not app.quitOnLastWindowClosed()
window.close()
QTest.qWait(50)
assert not window.isVisible() and not sip.isdeleted(window)
assert not client._stopped and not window._stopped
resident.open_action.trigger()
assert window.isVisible()
window.hide()
resident.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
assert window.isVisible()
resident.quit_action.trigger()
QTest.qWait(50)
assert client._stopped
assert sip.isdeleted(window)
''')

    def test_no_tray_keeps_a_visible_exit_path_and_close_stops_backend(self):
        self.run_resident('''
from PyQt6.QtWidgets import QLabel
FakeTray.available = False
resident = ResidentTray(window, tray_factory=FakeTray)
assert not resident.enabled and window.isVisible()
assert any('Closing this window will stop the app' in label.text() for label in window.findChildren(QLabel))
window.close()
QTest.qWait(50)
assert client._stopped
resident.shutdown()
''')

    def test_external_application_quit_cleans_up_a_hidden_resident(self):
        self.run_resident('''
resident = ResidentTray(window, tray_factory=FakeTray)
window.close()
fallback = []
def emergency_quit():
    fallback.append(True)
    resident.quit()
QTimer.singleShot(100, app.quit)
QTimer.singleShot(1500, emergency_quit)
assert app.exec() == 0
assert not fallback, 'The close-to-tray filter must not prevent application shutdown'
assert client._stopped
''')

    def test_tray_notices_deduplicate_epochs_and_only_open_current_confirmation(self):
        self.run_resident('''
resident = ResidentTray(window, tray_factory=FakeTray)
window.hide()
client.scenario('prompt')
assert len(resident.tray.messages) == 1
assert resident.tray.messages[0][0] == 'Tent mode is available'
epoch = client.state['pending_notification']
client.state['pending_notification'] = None
client.refresh()
client.state['pending_notification'] = epoch
client.refresh()
assert len(resident.tray.messages) == 1
client.state['session_connected'] = False
client.state['pending_notification'] = epoch + 1
client.refresh()
assert len(resident.tray.messages) == 1
resident.tray.messageClicked.emit()
assert window.isVisible()
assert client.state['manual'] == 'auto'
client.state['session_connected'] = True
client.refresh()
assert len(resident.tray.messages) == 2
resident.quit()
QTest.qWait(50)
''')

    def test_startup_ui_reads_back_and_reverts_failures_without_requiring_backend(self):
        self.run_ui('''
from panasonic_posture.windows.startup import LoginStartup
class Registry:
    value = None
    fail = False
    def read(self): return self.value
    def write(self, command):
        if self.fail: raise PermissionError('denied')
        self.value = command
    def delete(self):
        if self.fail: raise PermissionError('denied')
        self.value = None
registry = Registry()
startup = LoginStartup([sys.executable, 'fixed-entry.py'], registry=registry)
window.close()
window = Window(native_backend=True, client=UnavailableClient(), language='en', startup=startup)
window.show()
assert window.settings.startup_switch.isEnabled()
window.settings.startup_switch.setChecked(True)
assert registry.value == startup.command
registry.fail = True
window.settings.startup_switch.setChecked(False)
assert window.settings.startup_switch.isChecked()
registry.value = 'different program'
window.settings.refresh_startup()
assert not window.settings.startup_switch.isEnabled()
assert 'another app or location' in window.settings.startup_card.contentLabel.text()
registry.value = None
window.settings.refresh_startup()
window.unavailable()
assert window.settings.startup_switch.isEnabled()
''')

    def test_native_screen_mode_descriptions_and_capabilities_are_truthful(self):
        self.run_ui('''
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLabel
window.close()
client = DemoClient(QTimer.singleShot)
client.state.update(native_backend=True, input_control_available=False,
                    available_modes=['auto', 'laptop', 'tent', 'stand'])
window = Window(native_backend=True, client=client, language='en')
window.show()
QTest.qWait(50)
assert window.settings.radios['tent'].isEnabled()
assert not window.settings.radios['tablet'].isEnabled()
assert window.settings.mode_cards['tent'].contentLabel.text() == 'Turns the screen upside down'
assert window.settings.mode_cards['stand'].contentLabel.text() == 'Keeps the screen upright'
assert any('Keyboard and touchpad stay enabled' in label.text() for label in window.findChildren(QLabel))
''')

    def test_native_hardware_client_updates_widgets_via_the_wire_contract(self):
        self.run_ui('''
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from PyQt6.QtTest import QTest
from panasonic_posture.model import SensorInfo
from panasonic_posture.windows.backend import Backend
from panasonic_posture.windows.hardware_client import HardwareUIClient
from panasonic_posture.windows.hardware_host import HardwareHost, ConsoleIdentity
from panasonic_posture.windows.hardware_ipc import HardwareConnection
window.close()
with TemporaryDirectory() as directory:
    backend = Backend(Path(directory)/'settings.json', simulate=True)
    # Synthetic ports supply native-shaped snapshots for this widget test.
    # No SYSTEM process, sensor, display or input device is used here.
    backend.simulation.sensor.info = SensorInfo(ready=True, backend='windows-winrt')
    peer = ConsoleIdentity('S-1-5-21-1000', 100, 1, 42, 100000)
    host = HardwareHost(backend.simulation.engine, authorize=lambda value: value == peer)
    class Pipes:
        reply = None
        def connect(self, name):
            return 1
        def write(self, handle, request):
            self.reply = host.dispatch(peer, request)
            backend.advance(time.monotonic())
            self.reply['state'] = backend.simulation.engine.state.to_dict()
            self.reply['error'] = None
        def read(self, handle):
            reply, self.reply = self.reply, None
            return reply
        def close(self, handle):
            pass
    client = HardwareUIClient(factory=lambda: HardwareConnection(pipes=Pipes()))
    window = Window(hardware_backend=True, client=client, language='en')
    window.show()
    deadline = time.monotonic() + 8
    while window.settings.model.state is None and time.monotonic() < deadline:
        QTest.qWait(25)
    assert window.settings.model.state is not None
    assert window.settings.radios['tent'].isEnabled()
    window.settings.radios['tent'].click()
    deadline = time.monotonic() + 8
    while (window.settings.model.state['applied'] or {}).get('mode') != 'tent' and time.monotonic() < deadline:
        QTest.qWait(25)
    assert window.settings.model.state['applied']['mode'] == 'tent'
    assert window.home.heading.text() == 'Tent'
    assert not window.settings.model.state['simulation']
    window.close()
    client._thread.join(timeout=3)
    assert not client._thread.is_alive()
    backend.close()
''')

    def run_ui(self, body, scale='1'):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR=scale,
                   LANGUAGE='en', LANG='C.UTF-8')
        if sys.platform == 'win32':
            # The generic offscreen plugin does not discover Windows fonts.
            # Use the same installed fonts as the native Windows renderer.
            env.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts'))
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
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtTest import QTest
from panasonic_posture.fluent.app import Window
from panasonic_posture.fluent.demo import DemoClient, UnavailableClient
app = QApplication([])
assert QFontDatabase.families(), 'GUI verification requires installed system fonts'
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

    def test_return_to_automatic(self):
        self.run_ui('''
window.client.set_mode('tent')
QTest.qWait(50)
home = window.home
assert home.auto_button.isVisible()
home.auto_button.click()
assert window.client.state['manual'] == 'auto'
assert not home.auto_button.isVisible()
window.client.scenario('disconnected')
assert not home.auto_button.isVisible()
''')

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
assert window.settings.radios['tent'].isEnabled()
assert window.client.state['manual'] == 'tent'
assert window.home.auto_button.isVisibleTo(window.home)
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

    def test_unavailable_client_has_no_simulated_connection(self):
        self.run_ui('''
normal = Window(language='en', client=UnavailableClient())
assert isinstance(normal.client, UnavailableClient)
assert not normal.settings.radios['tent'].isEnabled()
assert 'not connected' in normal.home.detail.text().lower()
normal.close()
''')

    def test_private_backend_persists_preferences_and_disables_device_controls(self):
        self.run_ui('''
from pathlib import Path
from tempfile import TemporaryDirectory
import time
with TemporaryDirectory() as directory:
    path = Path(directory) / 'settings.json'
    connected = Window(language='zh_CN', backend=True, settings_path=path)
    connected.show()
    deadline = time.monotonic() + 8
    while connected.settings.model.state is None and time.monotonic() < deadline:
        QTest.qWait(25)
    assert connected.settings.model.state is not None
    assert connected.settings.switches['tent_enabled'].isEnabled()
    assert not connected.settings.radios['tent'].isEnabled()
    assert not connected.settings.calibrate_button.isEnabled()
    connected.settings.switches['tent_enabled'].setChecked(False)
    deadline = time.monotonic() + 8
    while connected.settings.model.value('tent_enabled') is not False and time.monotonic() < deadline:
        QTest.qWait(25)
    while connected.settings.model.active and time.monotonic() < deadline:
        QTest.qWait(25)
    assert path.is_file()
    assert not connected.settings.model.state['tent_enabled']
    assert connected.settings.model.state['applied'] is None
    process = connected.client.process
    connected.close()
    assert process.poll() is not None
''')

    def test_default_start_uses_platform_safe_client(self):
        self.run_ui('''
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from panasonic_posture.windows.client import ProcessClient
import os, time
with TemporaryDirectory() as directory, patch.dict(os.environ, LOCALAPPDATA=directory):
    normal = Window(language='en')
    normal.show()
    if sys.platform == 'win32':
        assert isinstance(normal.client, ProcessClient)
        deadline = time.monotonic() + 8
        while normal.settings.model.state is None and time.monotonic() < deadline:
            QTest.qWait(25)
        assert normal.settings.model.state is not None
        assert not normal.settings.model.state['supported']
        assert normal.settings.switches['tent_enabled'].isEnabled()
    else:
        assert isinstance(normal.client, UnavailableClient)
        assert normal.settings.model.state is None
    assert not normal.settings.radios['tent'].isEnabled()
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

    def test_wrapped_setting_compacts_when_window_widens(self):
        self.run_ui('''
window.switchTo(window.settings)
window.resize(420, 560)
QTest.qWait(200)
card = window.settings.sensor_card
wrapped = card.contentLabel.height()
window.resize(800, 660)
QTest.qWait(150)
assert card.contentLabel.height() < wrapped
assert card.contentLabel.height() >= card.contentLabel.fontMetrics().height()
''')

    def test_rapid_navigation_keeps_last_page_interactive(self):
        self.run_ui('''
window.navigation.items['settings'].click()
QTest.qWait(25)
window.navigation.items['posture'].click()
window.navigation.items['settings'].click()
assert window.stackedWidget.currentWidget() is window.settings
window.settings.radios['stand'].click()
QTest.qWait(450)
assert window.navigation.currentRouteKey() == 'settings'
assert window.home.heading.text() == 'Stand'
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
