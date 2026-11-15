"""Windows UI behavior with synthetic clients and optional isolated Qt widgets."""
import importlib.util
from contextlib import redirect_stderr
import io
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from panasonic_posture.windows.ui.unavailable import UnavailableClient, UnavailableError


class UnavailableClientTests(unittest.TestCase):
    def test_start_and_refresh_only_report_disconnection(self):
        client, unavailable = UnavailableClient(), []
        unexpected = lambda *args: self.fail('disconnected client published a connection or state')
        client.start(unexpected, lambda: unavailable.append(True), unexpected)
        client.refresh()
        client.stop()
        self.assertEqual(unavailable, [True, True])

    def test_all_operations_fail_without_synthesizing_success(self):
        client = UnavailableClient()
        for method, args in (
            ('set_mode', ('tent',)), ('set_preference', ('tent_enabled', False)),
            ('confirm_prompt', ('tent', 7)), ('calibrate', ()),
            ('reset_calibration', ()), ('retry', ('tent',)),
        ):
            with self.subTest(method=method):
                future = getattr(client, method)(*args)
                self.assertTrue(future.done())
                with self.assertRaises(UnavailableError):
                    future.result()


class CommandLineTests(unittest.TestCase):
    def test_cli_help_does_not_load_qt_or_gi(self):
        result = subprocess.run([sys.executable, '-S', '-m', 'panasonic_posture.windows.ui', '--help'],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--background', result.stdout)
        for retired in ('--demo', '--backend', '--scenario', '--settings'):
            self.assertNotIn(retired, result.stdout)

    def test_retired_development_options_are_not_accepted(self):
        from panasonic_posture.windows.ui.__main__ import main

        for option in ('--demo', '--backend', '--scenario', '--settings',
                       '--native', '--native-backend', '--hardware-backend', '--simulate-backend'):
            error = io.StringIO()
            with self.subTest(option=option), redirect_stderr(error):
                with self.assertRaises(SystemExit) as caught:
                    main([option])
                self.assertEqual(caught.exception.code, 2)
                self.assertIn('unrecognized arguments', error.getvalue())

    def test_background_is_windows_only(self):
        from panasonic_posture.windows.ui.__main__ import main

        error = io.StringIO()
        with patch('sys.platform', 'linux'), redirect_stderr(error):
            with self.assertRaises(SystemExit) as caught:
                main(['--background'])
        self.assertEqual(caught.exception.code, 2)
        self.assertIn('--background requires Windows', error.getvalue())


@unittest.skipUnless(importlib.util.find_spec('PyQt6') and importlib.util.find_spec('qfluentwidgets'),
                     'optional Fluent UI dependencies are not installed')
class FluentWidgetTests(unittest.TestCase):
    def test_lazy_window_keeps_shortcut_activation_and_repairs_reconnect_tooltip(self):
        self.run_resident('''
class Activation:
    pending = False
    def poll(self):
        value, self.pending = self.pending, False
        return value
activation = Activation()
resident = ResidentTray(None, window_factory=lambda: window, client=client,
                        activation=activation, tray_factory=FakeTray, language='en')
resident.show_window()
assert resident.activation_timer.isActive()
window.close()
assert not window.isVisible()
activation.pending = True
QTest.qWait(350)
assert window.isVisible()
resident.update_state(client.state)
expected = resident.tray.tooltip
resident.unavailable()
assert resident.tray.tooltip != expected
resident.update_state(client.state)
assert resident.tray.tooltip == expected
resident.quit()
''')

    def test_quit_waits_for_session_recovery_and_keeps_ui_on_failure(self):
        self.run_resident('''
from concurrent.futures import Future
pending = []
def stop_control():
    future = Future()
    pending.append(future)
    return future
client.stop_control = stop_control
resident = ResidentTray(window, tray_factory=FakeTray)
resident.quit()
assert not client._stopped and not resident.quitting
pending[-1].set_result(False)
QTest.qWait(150)
assert len(pending) == 2 and not client._stopped
import unittest
with unittest.TestCase().assertLogs('panasonic_posture.windows.ui.resident', level='ERROR'):
    pending[-1].set_exception(OSError('recovery failed'))
assert resident.quit_action.isEnabled() and not resident.quitting
assert resident.tray.messages[-1][0] == 'Unable to complete the operation'
resident.quit()
pending[-1].set_result(True)
assert client._stopped and resident.quitting
''')

    def test_reduced_motion_falls_back_before_qt_accessibility_api(self):
        self.run_ui('''
from panasonic_posture.windows.ui.app import _prefers_reduced_motion
class LegacyStyleHints:
    pass
assert not _prefers_reduced_motion(LegacyStyleHints())
''')

    def test_application_name_follows_the_windows_ui_language(self):
        self.run_ui('''
from panasonic_posture.windows.ui.app import configure_application
from panasonic_posture.windows.ui.app import Window
from panasonic_posture.windows.ui.unavailable import UnavailableClient
from panasonic_posture.product import APPLICATION_DISPLAY_NAME
assert configure_application('zh_CN') == '松下二合一平板电脑姿态管理工具'
assert app.applicationName() == '松下二合一平板电脑姿态管理工具'
assert app.applicationDisplayName() == '松下二合一平板电脑姿态管理工具'
window.close()
window = Window(client=UnavailableClient(), language='zh_CN')
assert window.windowTitle() == app.applicationDisplayName()
assert configure_application('en') == APPLICATION_DISPLAY_NAME
assert app.applicationName() == APPLICATION_DISPLAY_NAME
assert app.applicationDisplayName() == APPLICATION_DISPLAY_NAME
''')

    def test_home_shows_stable_error_reasons_and_action_failures(self):
        self.run_ui('''
from concurrent.futures import Future
from panasonic_posture.contracts import ErrorCode
from qfluentwidgets import InfoBar
from PyQt6.QtWidgets import QLabel
class StubBar:
    def __init__(self):
        self.titleLabel = QLabel()
        self.contentLabel = QLabel()
client = window.client
errors = []
InfoBar.error = lambda title, content, **kwargs: (
    errors.append((title, content)) or StubBar())
client.state['error_code'] = ErrorCode.DISPLAY_TRANSFORM
client.refresh()
assert window.home.detail.text() == (
    'Mode change failed: The screen rotation could not be applied. Please try again.'
)
client.state['error_code'] = None
client.scenario('prompt')
def fail(*args):
    result = Future()
    result.set_exception(RuntimeError(ErrorCode.DISPLAY_LOCK))
    return result
client.confirm_prompt = fail
window.home.confirm()
assert window.home.detail.text() == (
    'Mode change failed: The rotation lock could not be changed. Please try again.'
)
assert errors[-1] == (
    'Unable to complete the operation',
    'The rotation lock could not be changed. Please try again.',
)
''')

    def test_retry_button_reports_an_in_progress_request(self):
        self.run_ui('''
from concurrent.futures import Future
client = window.client
client.state['error_code'] = 'display-unavailable'
client.refresh()
pending = Future()
client.retry = lambda fallback_mode=None: pending
window.home.retry_button.click()
assert window.home.retry_button.text() == 'Retrying…'
assert not window.home.retry_button.isEnabled()
pending.set_result(None)
assert window.home.retry_button.text() == 'Retry'
''')

    def test_settings_failures_show_stable_error_reason(self):
        self.run_ui('''
from panasonic_posture.contracts import ErrorCode
from qfluentwidgets import InfoBar
from PyQt6.QtWidgets import QLabel
class StubBar:
    def __init__(self):
        self.titleLabel = QLabel()
        self.contentLabel = QLabel()
errors = []
InfoBar.error = lambda title, content, **kwargs: (
    errors.append((title, content)) or StubBar())
window.settings.action_failed(RuntimeError(ErrorCode.CONFIG_WRITE))
assert errors[-1] == (
    'Unable to complete the operation',
    'The setting could not be saved. Please try again.',
)
window.settings.action_failed(RuntimeError('PRIVATE diagnostic detail'))
assert errors[-1] == (
    'Unable to complete the operation',
    'Something went wrong. Please try again.',
)
assert all('PRIVATE' not in content for _, content in errors)
''')

    def test_hardware_preflight_explains_local_session_rejection(self):
        self.run_ui('''
from panasonic_posture.windows.ui.app import hardware_start_failure
from PyQt6.QtWidgets import QMessageBox
messages = []
QMessageBox.critical = lambda parent, title, text: messages.append((title, text))
assert hardware_start_failure('en', 'session') == 1
assert messages[-1] == (
    'Hardware mode is unavailable',
    'Run from the active local desktop with a normal user account.',
)
try:
    hardware_start_failure('en', 'unknown')
except ValueError:
    pass
else:
    raise AssertionError('unknown preflight failures must be rejected')
''')

    def test_about_repository_and_update_check_use_shared_user_copy(self):
        self.run_ui('''
from PyQt6.QtCore import QObject, QByteArray, QUrl, Qt, QTimer, pyqtSignal
from qfluentwidgets import PrimaryPushButton, SettingCard
from panasonic_posture.windows.ui.app import Window
from panasonic_posture.windows.ui.pages import APP_VERSION
from panasonic_posture.windows.ui.update_check import UpdateChecker
from panasonic_posture.windows.app.update_check import UpdateInfo
from windows_ui_fixture import SyntheticClient

latest_version = '99.0.0'

class FakeChecker(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal()
    def __init__(self):
        super().__init__()
        self.calls = 0
        self.closed = False
    def check(self):
        self.calls += 1
        self.completed.emit(UpdateInfo(APP_VERSION, latest_version, True))
        return True
    def shutdown(self):
        self.closed = True

checker = FakeChecker()
window.close()
window = Window(client=SyntheticClient(QTimer.singleShot), language='en', update_checker=checker)
window.show()
assert window.settings.github_button.getUrl().toString() == (
    'https://github.com/adogecheems/panasonic-posture-manager'
)
assert isinstance(window.settings.github_button.parent(), SettingCard)
link_item = window.settings.github_card.hBoxLayout.itemAt(
    window.settings.github_card.hBoxLayout.indexOf(window.settings.github_button)
)
assert link_item.alignment() & Qt.AlignmentFlag.AlignVCenter
assert isinstance(window.settings.update_button, PrimaryPushButton)
assert isinstance(window.settings.calibrate_button, PrimaryPushButton)
window.settings.update_button.click()
assert checker.calls == 1
assert window.settings.version_card.contentLabel.text() == (
    f'{APP_VERSION} · New version available: {latest_version}'
)
checker.failed.emit()
assert window.settings.version_card.contentLabel.text() == (
    f'{APP_VERSION} · Unable to check for updates. Try again later.'
)
checker.check = lambda: False
window.settings.update_button.click()
assert window.settings.update_button.isEnabled()
assert window.settings.version_card.contentLabel.text() == (
    f'{APP_VERSION} · Unable to check for updates. Try again later.'
)
window.close()
assert checker.closed

class Reply(QObject):
    finished = pyqtSignal()
    def __init__(self, status, payload, location=b''):
        super().__init__()
        self.status, self.payload, self.location = status, payload, location
        self.closed = False
    def attribute(self, attribute):
        return self.status
    def readAll(self):
        return QByteArray(self.payload)
    def rawHeader(self, name):
        return QByteArray(self.location if name == b'--location' else b'')
    def url(self):
        return QUrl('https://proxy.mmoe.work/http/fallback')
    def abort(self):
        pass
    def deleteLater(self):
        self.closed = True

class Manager:
    def __init__(self, replies):
        self.replies, self.requests, self.reply = replies, [], None
    def get(self, request):
        self.requests.append(request)
        self.reply = self.replies.pop(0)
        return self.reply

manager = Manager([Reply(200, b'{"tag_name":"v2.5.0"}')])
network_checker = UpdateChecker('2.4.0', manager=manager)
results = []
network_checker.completed.connect(results.append)
assert network_checker.check()
assert manager.requests[-1].url().toString() == (
    'https://proxy.mmoe.work/http/'
    'https://api.github.com/repos/adogecheems/panasonic-posture-manager/releases/latest'
)
assert bytes(manager.requests[-1].rawHeader(b'Referer')).decode() == (
    'https://proxy.mmoe.work/'
)
manager.reply.finished.emit()
assert results[-1].update_available
assert results[-1].latest == '2.5.0'

manager = Manager([
    Reply(403, b'{"message":"rate limit"}'),
    Reply(312, b'', b'https://github.com/adogecheems/panasonic-posture-manager/releases/tag/v2.5.1'),
])
network_checker = UpdateChecker('2.4.0', manager=manager)
results = []
network_checker.completed.connect(results.append)
assert network_checker.check()
manager.reply.finished.emit()
assert len(manager.requests) == 2
assert manager.requests[-1].url().toString() == (
    'https://proxy.mmoe.work/http/'
    'https://github.com/adogecheems/panasonic-posture-manager/releases/latest'
)
manager.reply.finished.emit()
assert results[-1].latest == '2.5.1'

failed = []
manager = Manager([Reply(403, b''), Reply(403, b'')])
network_checker = UpdateChecker('2.4.0', manager=manager)
network_checker.failed.connect(lambda: failed.append(True))
assert network_checker.check()
manager.reply.finished.emit()
manager.reply.finished.emit()
assert failed == [True]
network_checker.shutdown()
''')

    def run_resident(self, body):
        self.run_ui('''
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import QLabel, QSystemTrayIcon
from panasonic_posture.windows.ui.resident import ResidentTray
class FakeTray(QObject):
    available = True
    activated = pyqtSignal(object)
    messageClicked = pyqtSignal()
    def __init__(self, icon, parent):
        super().__init__(parent)
        self.icon = icon
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
client = SyntheticClient(QTimer.singleShot)
client.state.update(input_control_available=False,
                    available_modes=['auto', 'laptop', 'tent', 'stand'])
window = Window(client=client, language='en')
window.show()
''' + body)

    def test_resident_close_retains_backend_reopen_and_explicit_quit_stops_it(self):
        self.run_resident('''
from PyQt6 import sip
resident = ResidentTray(window, tray_factory=FakeTray)
assert resident.enabled and resident.tray.visible
assert not resident.tray.icon.pixmap(16, 16).isNull()
assert resident.tray.icon.cacheKey() != window.windowIcon().cacheKey()
assert resident.tray.icon.cacheKey() == resident.icon.cacheKey()
assert not app.quitOnLastWindowClosed()
assert not any('system tray' in label.text().lower() for label in window.findChildren(QLabel))
window.close()
QTest.qWait(50)
assert not window.isVisible() and not sip.isdeleted(window)
assert not client._stopped and not window._stopped
resident.open_action.trigger()
assert window.isVisible()
window.hide()
resident.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
QTest.qWait(250)
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

    def test_tray_notices_deduplicate_epochs_and_confirm_only_the_current_notice(self):
        self.run_resident('''
resident = ResidentTray(window, tray_factory=FakeTray)
window.hide()
client.scenario('prompt')
assert len(resident.tray.messages) == 1
assert resident.tray.messages[0][0] == 'Tent mode is available'
assert isinstance(resident.tray.messages[0][2], QIcon)
assert resident.tray.messages[0][2].cacheKey() == resident.icon.cacheKey()
notice = resident.tray.messages[0][2].pixmap(256, 256).toImage()
original = window.windowIcon().pixmap(256, 256).toImage()
assert notice.pixelColor(0, 0).getRgb() == (255, 255, 255, 255)
assert notice.pixelColor(0, 0).alpha() == 255
assert min(notice.pixelColor(x, y).alpha() for y in range(0, notice.height(), 16)
           for x in range(0, notice.width(), 16)) == 255
center = notice.width() // 2
notice_center, original_center = notice.pixelColor(center, center), original.pixelColor(center, center)
assert max(abs(a - b) for a, b in zip(notice_center.getRgb()[:3], original_center.getRgb()[:3])) <= 4
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
assert client.state['pending_notification'] == epoch + 1
window.hide()
resident.tray.messageClicked.emit()
QTest.qWait(450)
assert not window.isVisible()
assert client.state['manual'] == 'tent'
assert window.home.heading.text() == 'Tent'
resident.quit()
QTest.qWait(50)
''')

    def test_one_notice_click_switches_without_opening_or_confirming_twice(self):
        self.run_resident('''
resident = ResidentTray(window, tray_factory=FakeTray)
window.hide()
client.scenario('prompt')
events = []
resident.show_window = lambda: events.append('show')
window.home.confirm = lambda: events.append('confirm')
resident.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
resident.tray.messageClicked.emit()
assert events == ['confirm']
assert resident.notice is None
assert not window.isVisible()
QTest.qWait(800)
resident.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
QTest.qWait(250)
assert events == ['confirm', 'show']
resident.quit()
QTest.qWait(50)
''')

    def test_tray_icon_opens_the_window_without_confirming_an_available_notice(self):
        self.run_resident('''
resident = ResidentTray(window, tray_factory=FakeTray)
window.hide()
client.scenario('prompt')
events = []
resident.show_window = lambda: events.append('show')
window.home.confirm = lambda: events.append('confirm')
resident.tray.activated.emit(QSystemTrayIcon.ActivationReason.Trigger)
QTest.qWait(250)
assert events == ['show']
assert resident.notice is not None
assert client.state['manual'] == 'auto'
assert client.state['pending_notification'] is not None
resident.quit()
QTest.qWait(50)
''')

    def test_notice_click_opens_the_window_while_another_action_is_busy(self):
        self.run_resident('''
resident = ResidentTray(window, tray_factory=FakeTray)
window.hide()
client.scenario('prompt')
events = []
resident.show_window = lambda: events.append('show')
window.home.confirm = lambda: events.append('confirm')
window.home.actions.busy = 'retry'
resident.tray.messageClicked.emit()
assert events == ['show']
assert resident.notice is not None
resident.quit()
QTest.qWait(50)
''')

    def test_notice_click_survives_a_temporary_confirmability_gap(self):
        self.run_resident('''
resident = ResidentTray(window, tray_factory=FakeTray)
window.hide()
client.scenario('prompt')
epoch = client.state['pending_notification']
client.state['pending_notification'] = None
client.refresh()
assert resident.notice is not None
client.state['pending_notification'] = epoch
client.refresh()
resident.tray.messageClicked.emit()
QTest.qWait(450)
assert not window.isVisible()
assert client.state['manual'] == 'tent'
resident.quit()
QTest.qWait(50)
''')

    def test_lazy_resident_confirms_a_notice_before_loading_the_window(self):
        self.run_resident('''
from concurrent.futures import Future
from copy import deepcopy
from panasonic_posture.windows.ui.app import Window

class LazyClient:
    def __init__(self):
        self.started = self.stopped = self.polls = 0
        self.confirmations = []
        self.fail_confirmation = False
        self.on_state = self.on_unavailable = None
    def start(self, on_state, on_unavailable):
        self.started += 1
        self.on_state, self.on_unavailable = on_state, on_unavailable
    def poll(self):
        self.polls += 1
    def refresh(self):
        self.polls += 1
    def stop(self):
        self.stopped += 1
    def confirm_prompt(self, prompt, epoch):
        self.confirmations.append((prompt, epoch))
        future = Future()
        if self.fail_confirmation:
            future.set_exception(RuntimeError('confirmation failed'))
        else:
            future.set_result(None)
        return future

state = deepcopy(client.state)
state.update(
    instance='lazy-instance',
    pending_notification=7,
    pending_prompt='tent',
    pose='tent',
    supported=True,
    session_connected=True,
    error_code=None,
    hardware=False,
    applied={'mode': 'laptop'},
)
client = LazyClient()
made = []
def factory():
    made.append(Window(client=client, language='en', start_client=False))
    return made[-1]

resident = ResidentTray(None, window_factory=factory, client=client,
                       tray_factory=FakeTray, language='en')
assert resident.enabled and resident.tray.visible
assert client.started == 1 and made == []
client.on_state(state)
assert len(resident.tray.messages) == 1
resident.open_notice()
assert client.confirmations == [('tent', 7)]
assert resident.notice is None and made == []
state['pending_notification'] = 8
client.fail_confirmation = True
client.on_state(state)
resident._notice_click_until = 0
resident.open_notice()
assert resident.notice is not None
assert resident.tray.messages[-1][0] == 'Unable to complete the operation'
assert made == []
resident.show_window()
assert len(made) == 1 and resident.window._last_state == state
assert client.started == 1 and not resident.timer.isActive()
resident.quit()
QTest.qWait(50)
assert client.stopped >= 1
''')

    def test_startup_ui_reads_back_and_reverts_failures_without_requiring_backend(self):
        self.run_ui('''
from panasonic_posture.windows.app.startup import LoginStartup
class Registry:
    value = None
    fail = False
    initialized = False
    def read(self): return self.value
    def write(self, command):
        if self.fail: raise PermissionError('denied')
        self.value = command
    def delete(self):
        if self.fail: raise PermissionError('denied')
        self.value = None
    def read_initialized(self): return self.initialized
    def mark_initialized(self): self.initialized = True
registry = Registry()
startup = LoginStartup([sys.executable, 'fixed-entry.py'], registry=registry)
window.close()
window = Window(client=UnavailableClient(), language='en', startup=startup)
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
assert window.settings.startup_card.contentLabel.text() == 'Start in the system tray when you sign in.'
window.unavailable()
assert window.settings.startup_switch.isEnabled()
''')

    def test_hardware_mode_descriptions_and_capabilities_are_truthful(self):
        self.run_ui('''
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QLabel
window.close()
client = SyntheticClient(QTimer.singleShot)
client.state.update(input_control_available=False,
                    available_modes=['auto', 'laptop', 'tent', 'stand'])
window = Window(client=client, language='en')
window.show()
QTest.qWait(50)
assert window.settings.radios['tent'].isEnabled()
assert 'tablet' not in window.settings.radios
assert window.settings.mode_cards['laptop'].contentLabel.text() == 'Best for typing and precise work'
assert window.settings.mode_cards['tent'].contentLabel.text() == 'Designed for use in tent form'
assert window.settings.mode_cards['stand'].contentLabel.text() == 'Designed for viewing in stand form'
assert not any('Windows' in label.text() or 'screen rotation' in label.text().lower()
               for label in window.findChildren(QLabel))
assert not any('Compatibility mode' in label.text() for label in window.findChildren(QLabel))
''')

    def test_native_hardware_client_updates_widgets_via_the_wire_contract(self):
        self.run_ui('''
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from PyQt6.QtTest import QTest
from panasonic_posture.model import SensorInfo
from panasonic_posture.service.settings_store import SettingsStore
from panasonic_posture.windows.ipc.client import HardwareUIClient
from panasonic_posture.windows.service.host import HardwareHost, ConsoleIdentity
from panasonic_posture.windows.service.ipc import HardwareConnection, PipePoll
from windows_fixture import Simulation
window.close()
with TemporaryDirectory() as directory:
    simulation = Simulation(
        SettingsStore(Path(directory) / 'settings.json'),
        'fluent-hardware-fixture',
    )
    # Synthetic ports supply native-shaped snapshots for this widget test.
    # No SYSTEM process, sensor, display or input device is used here.
    simulation.sensor.info = SensorInfo(ready=True, backend='windows-winrt')
    peer = ConsoleIdentity('S-1-5-21-1000', 100, 1, 42, 100000)
    host = HardwareHost(simulation.engine, authorize=lambda value: value == peer)
    class Pipes:
        reply = None
        def connect(self, name):
            return 1
        def write(self, handle, request):
            self.reply = host.dispatch(peer, request)
            simulation.advance(time.monotonic())
            self.reply['state'] = simulation.engine.state.to_dict()
            self.reply['error'] = None
            return PipePoll.ready()
        def read(self, handle):
            reply, self.reply = self.reply, None
            return PipePoll.ready(reply) if reply is not None else PipePoll.idle()
        def close(self, handle):
            pass
    client = HardwareUIClient(factory=lambda: HardwareConnection(pipes=Pipes()))
    window = Window(client=client, language='en')
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
    window.close()
    client._thread.join(timeout=3)
    assert not client._thread.is_alive()
    simulation.close()
''')

    def run_ui(self, body, scale='1'):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen', QT_SCALE_FACTOR=scale,
                   LANGUAGE='en', LANG='C.UTF-8')
        if sys.platform == 'win32':
            # The generic offscreen plugin does not discover Windows fonts.
            # Use the same installed fonts as the native Windows renderer.
            env.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts'))
        root = Path(__file__).resolve().parents[1]
        python_path = [str(root / 'src')]
        ui_dependencies = os.environ.get('PANASONIC_POSTURE_UI_DEPENDENCIES')
        if ui_dependencies:
            python_path.extend(
                path for path in ui_dependencies.split(os.pathsep) if path
            )
        env['PYTHONPATH'] = os.pathsep.join(python_path)
        prelude = f'''
import os, sys
for directory in os.environ.get('PANASONIC_POSTURE_DLL_DIRECTORIES', '').split(os.pathsep):
    if directory:
        os.add_dll_directory(directory)
sys.path[:0] = [{str(root / "src")!r}, {str(root / "tests")!r}]
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == 'gi' or name.startswith('panasonic_posture.linux'):
            raise AssertionError(name)
sys.meta_path.insert(0, Block())
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtTest import QTest
from PyQt6.QtCore import QTimer
from panasonic_posture.windows.ui.app import Window
from panasonic_posture.windows.ui.unavailable import UnavailableClient
from windows_ui_fixture import SyntheticClient
app = QApplication([])
assert QFontDatabase.families(), 'GUI verification requires installed system fonts'
window = Window(client=SyntheticClient(QTimer.singleShot), language='en')
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
assert 'test' not in normal.home.detail.text().lower()
normal.close()
''')

    def test_startup_does_not_flash_connecting_copy(self):
        self.run_ui('''
class DelayedClient:
    def start(self, on_state, on_unavailable, on_connected=None):
        self.on_state, self.on_unavailable = on_state, on_unavailable
        self._on_connected = on_connected
    def refresh(self):
        pass
    def stop(self):
        pass
window.close()
window = Window(client=DelayedClient(), language='en')
window.show()
QTest.qWait(50)
assert window.home.detail.text() == ''
assert not window.home.detail.isVisible()
window.unavailable()
assert 'not connected' in window.home.detail.text().lower()
assert window.home.detail.isVisible()
''')

    def test_backend_state_reconciles_once_per_second(self):
        self.run_ui('''
from panasonic_posture.contracts import TICK_SECONDS
from panasonic_posture.windows.ipc.client import HardwareUIClient
class PollingClient(HardwareUIClient):
    def __init__(self):
        super().__init__()
        self.polls = 0
        self.refreshes = 0
    def start(self, on_state, on_unavailable, on_connected=None):
        pass
    def poll(self):
        self.polls += 1
    def refresh(self):
        self.refreshes += 1
    def stop(self):
        pass
window.close()
client = PollingClient()
window = Window(client=client, language='en')
ticks_per_second = max(1, round(1000 / max(1, round(TICK_SECONDS * 1000))))
for _ in range(ticks_per_second):
    window.poll_backend()
assert client.polls == ticks_per_second
assert client.refreshes == 1
window.close()
''')

    def test_unchanged_presentation_skips_widget_work_and_reuses_svg(self):
        self.run_ui('''
import time
import panasonic_posture.windows.ui.widgets as widget_module
client = window.client
home_calls = []
settings_fits = []
illustration_updates = []
original_home = window.home.update_state
original_fit = window.settings.fit_cards
window.home.update_state = lambda state: (home_calls.append(True), original_home(state))
window.settings.fit_cards = lambda: (settings_fits.append(True), original_fit())
window.home.illustration.update = lambda: illustration_updates.append(True)

current = dict(client.state)
window.update_state(current)
window.update_state(dict(current))
assert not home_calls, 'An identical snapshot must not redraw either page'

unrelated = dict(current, generation=current['generation'] + 1,
                 applied={'mode': 'tent'})
window.update_state(unrelated)
QTest.qWait(20)
assert len(home_calls) == 1
assert not illustration_updates, 'An unchanged pose must not request another SVG paint'
assert not settings_fits, 'Unrelated state fields must not relayout settings cards'

relevant = dict(unrelated, tent_enabled=not unrelated['tent_enabled'])
window.update_state(relevant)
QTest.qWait(20)
assert len(settings_fits) == 1

renderers = []
render_instances = []
real_renderer = widget_module.QSvgRenderer
class CountingRenderer:
    def __init__(self, payload):
        renderers.append(payload)
        render_instances.append(self)
        self.render_calls = 0
        self.renderer = real_renderer(payload)
    def render(self, *args):
        self.render_calls += 1
        return self.renderer.render(*args)
widget_module.QSvgRenderer = CountingRenderer
window.home.illustration._renderers.clear()
window.home.illustration.pose = 'tent'
window.home.illustration.update()
deadline = time.monotonic() + 2
while not render_instances and time.monotonic() < deadline:
    QTest.qWait(10)
assert render_instances, 'The changed pose must request an SVG paint'
# Repaint synchronously so renderer reuse is independent of Qt's update
# coalescing in the offscreen platform plugin.
window.home.illustration.repaint()
assert len(renderers) == 1, 'The same pose and theme must reuse one SVG renderer'
assert sum(instance.render_calls for instance in render_instances) >= 2
''')

    def test_hardware_backend_failure_has_actionable_message(self):
        self.run_ui('''
client = UnavailableClient()
client.startup_failure = 'backend-startup-failed'
hardware = Window(client=client, language='en')
assert hardware.home.detail.text() == (
    'The background service could not start. Please try again.'
)
hardware.close()
''')

    def test_default_start_uses_platform_safe_client(self):
        self.run_ui('''
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from panasonic_posture.contracts import TICK_SECONDS
from panasonic_posture.windows.ipc.client import HardwareUIClient
from panasonic_posture.windows.ui.unavailable import UnavailableClient
import os, time
with TemporaryDirectory() as directory, patch.dict(os.environ, LOCALAPPDATA=directory):
    normal = Window(language='en')
    normal.show()
    if sys.platform == 'win32':
        assert isinstance(normal.client, HardwareUIClient)
        assert normal.settings.model.state is None
        assert normal.backend_timer.interval() == max(1, round(TICK_SECONDS * 1000))
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

    def test_appearance_selector_tracks_configured_theme(self):
        self.run_ui('''
from qfluentwidgets import ComboBox, Theme, qconfig, setTheme
setTheme(Theme.DARK)
second = Window(client=SyntheticClient(QTimer.singleShot), language='en')
first_selector = window.settings.findChild(ComboBox)
second_selector = second.settings.findChild(ComboBox)
assert second_selector.currentText() == 'Dark'
assert first_selector.currentText() == 'Dark'
second_selector.activated.emit(1)
assert qconfig.get(qconfig.themeMode) == Theme.LIGHT
assert first_selector.currentText() == 'Light'
setTheme(Theme.AUTO)
assert second_selector.currentText() == 'System'
second.close()
''')

    def test_chinese_dark_and_minimum_layout(self):
        self.run_ui('''
from qfluentwidgets import setTheme, Theme
translated = Window(client=SyntheticClient(QTimer.singleShot), language='zh_CN')
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
from qfluentwidgets import fontFamilies, setTheme, Theme
from PyQt6.QtGui import QFont, QIcon
from panasonic_posture.windows.app.resources import asset_root
assert (window.width(), window.height()) == (500, 660)
assert window.stackedWidget.count() == 1  # Settings page builds on first use
assert not hasattr(window, 'navigationInterface')
assert window.stackedWidget.currentWidget() is window.home
assert app.font().hintingPreference() == QFont.HintingPreference.PreferDefaultHinting
assert app.font().styleStrategy() == QFont.StyleStrategy.PreferAntialias
assert fontFamilies() == app.font().families()
assert window.titleBar.titleLabel.font().families() == fontFamilies()
assert window.titleBar.titleLabel.font().pixelSize() == 13
assert window.titleBar.hBoxLayout.itemAt(1).spacerItem().sizeHint().width() == 6
margins = window.titleBar.titleLabel.contentsMargins()
assert (margins.top(), margins.bottom()) == (1, 4)
window.navigation.items['settings'].click()
assert window.stackedWidget.count() == 2
assert window.stackedWidget.currentWidget() is window.settings
assert window.navigation.currentRouteKey() == 'settings'
app_icon = QIcon(str(asset_root() / 'icons/work.mmoe.panasonic.PostureManager.png'))
for theme, filename in ((Theme.LIGHT, 'lets-note-window-icon.png'),
                        (Theme.DARK, 'lets-note-window-icon-dark.png')):
    setTheme(theme)
    QTest.qWait(50)
    assert window.windowIcon().pixmap(72, 42).toImage() == app_icon.pixmap(72, 42).toImage()
    mark = window.titleBar.iconLabel.pixmap()
    assert mark.toImage() != window.windowIcon().pixmap(mark.width(), mark.height()).toImage()
    assert mark.width() >= 72 * window.devicePixelRatioF()
    assert abs(mark.deviceIndependentSize().width() - 72) < 0.1
    assert mark.deviceIndependentSize().height() > 35
window.switchTo(window.home)
assert window.home.verticalScrollBar().maximum() == 0
''')

    def test_dialogs_and_banners_use_the_app_text_roles(self):
        self.run_ui('''
from PyQt6.QtGui import QFont
from qfluentwidgets import InfoBar, MessageBox, Theme, setTheme
from panasonic_posture.windows.ui.widgets import (
    BODY_SIZE, SECTION_TITLE_SIZE, EMPHASIS_WEIGHT,
    DIALOG_TITLE_SIZE, DIALOG_BODY_SIZE,
    TEXT_LIGHT, TEXT_DARK, MUTED_LIGHT, MUTED_DARK,
)

def effective(label):
    label.ensurePolished()
    return label.font().pixelSize(), label.font().weight()

def center_y(widget, parent):
    origin = widget.mapTo(parent, widget.rect().topLeft())
    return origin.y() + widget.height() / 2

window.notify(True)
bar = window.findChildren(InfoBar)[-1]
assert effective(bar.titleLabel) == (SECTION_TITLE_SIZE, EMPHASIS_WEIGHT)
assert effective(bar.contentLabel) == (BODY_SIZE, QFont.Weight.Normal)
assert bar.titleLabel.font().families() == app.font().families()
assert bar.height() < 40, 'the notice bar must stay slim'
assert bar.iconWidget.width() == 24
assert bar.closeButton.width() == 28
for item in (bar.titleLabel, bar.iconWidget, bar.closeButton):
    assert abs(center_y(item, bar) - bar.height() / 2) <= 2
title = bar.titleLabel.mapTo(bar, bar.titleLabel.rect().topLeft())
icon = bar.iconWidget.mapTo(bar, bar.iconWidget.rect().topLeft())
close = bar.closeButton.mapTo(bar, bar.closeButton.rect().topLeft())
left_gap = title.x() - (icon.x() + bar.iconWidget.width())
right_gap = close.x() - (title.x() + bar.titleLabel.width())
assert 6 <= left_gap <= 10, 'the copy keeps a small gap from the glyph'
assert right_gap <= 14, 'the copy must hug the close button'
assert abs(left_gap - right_gap) <= 6, 'the copy sits between even gaps'
window.notify(False, 'Please try again.')
error_bar = window.findChildren(InfoBar)[-1]
assert effective(error_bar.titleLabel) == (SECTION_TITLE_SIZE, EMPHASIS_WEIGHT)
assert effective(error_bar.contentLabel) == (BODY_SIZE, QFont.Weight.Normal)
assert error_bar.contentLabel.font().families() == app.font().families()
assert error_bar.height() < 40
for item in (error_bar.titleLabel, error_bar.contentLabel,
             error_bar.iconWidget, error_bar.closeButton):
    assert abs(center_y(item, error_bar) - error_bar.height() / 2) <= 2
error_title = error_bar.titleLabel.mapTo(error_bar, error_bar.titleLabel.rect().topLeft())
error_content = error_bar.contentLabel.mapTo(error_bar, error_bar.contentLabel.rect().topLeft())
error_icon = error_bar.iconWidget.mapTo(error_bar, error_bar.iconWidget.rect().topLeft())
error_close = error_bar.closeButton.mapTo(error_bar, error_bar.closeButton.rect().topLeft())
assert error_title.x() - (error_icon.x() + error_bar.iconWidget.width()) == 8
assert error_close.x() - (error_content.x() + error_bar.contentLabel.width()) <= 14

opened = []
MessageBox.exec = lambda self: opened.append(self) or False
window.settings.ask_calibration()
assert opened, 'the calibration dialog must be created through the shared typography'
dialog = opened[0]
assert effective(dialog.titleLabel) == (DIALOG_TITLE_SIZE, QFont.Weight.Normal)
assert effective(dialog.contentLabel) == (DIALOG_BODY_SIZE, QFont.Weight.Normal)
assert dialog.contentLabel.font().families() == app.font().families()
# The title keeps the app text color hierarchy instead of the stock black.
assert TEXT_LIGHT[1:] in dialog.titleLabel.styleSheet()
assert MUTED_LIGHT[1:] in dialog.contentLabel.styleSheet()
assert dialog.buttonGroup.height() == 68
assert dialog.textLayout.contentsMargins().bottom() == 16
bottom = dialog.contentLabel.geometry().bottom() + 1
assert dialog.widget.height() <= bottom + 16 + 68 + 2, 'the frame tracks the compact insets'
assert dialog.widget.height() < bottom + 105, 'the stock bottom allowance must be removed'
setTheme(Theme.DARK)
QTest.qWait(50)
assert TEXT_DARK[1:] in dialog.titleLabel.styleSheet()
assert MUTED_DARK[1:] in dialog.contentLabel.styleSheet()
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
title_x = first.titleLabel.mapTo(window.settings, QPoint()).x()
for card, controls in window.settings.cards:
    assert card.titleLabel.mapTo(window.settings, QPoint()).x() == title_x
    if controls:
        control = controls[-1]
        inset = card.contentsRect().right() - control.geometry().right()
        assert inset == 16, (card.titleLabel.text(), inset)
for group in window.settings.groups:
    label = group.titleLabel
    assert label.mapTo(window.settings, label.contentsRect().topLeft()).x() == title_x
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
    assert window.settings.reset_button.palette().buttonText().color() == primary
    assert window.titleBar.titleLabel.palette().windowText().color() == secondary
    assert window.settings.groups[0].titleLabel.palette().windowText().color() == secondary
''')
