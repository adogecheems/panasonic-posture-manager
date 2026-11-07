"""Python UI contracts and real widget checks on a private Broadway display."""
from concurrent.futures import Future
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from panasonic_posture.contracts import ALL_ERROR_CODES
from panasonic_posture.model import StateSnapshot, Target
from panasonic_posture.ui.errors import describe_error, ServiceUnavailable
from panasonic_posture.session.errors import ERROR_MESSAGES, error_text
from panasonic_posture.ui.service import PostureService, parse_state
from panasonic_posture.ui.common import DATA_ROOT
from gi.repository import GLib

ROOT = Path(__file__).resolve().parents[1]


def state(**values):
    result = StateSnapshot().to_dict()
    result.update(generation=1, target=Target(mode='laptop', tablet=False, rotation=0).to_dict(), session_connected=True,
                  supported=True, support_tier='validated', sensor_source='sysfs', hardware=False)
    result.update(values)
    return result


class FakeService(PostureService):
    def __init__(self):
        self.calls = []
        self.refreshes = 0
        self.stopped = False

    def call(self, method, parameters=None):
        future = Future()
        self.calls.append((method, parameters.unpack() if parameters else None, future))
        return future

    def retry(self, mode):
        return self.call('Retry')

    def refresh(self):
        self.refreshes += 1

    def start(self, on_state, on_unavailable):
        self.on_state, self.on_unavailable = on_state, on_unavailable

    def stop(self):
        self.stopped = True


class ProtocolTests(unittest.TestCase):
    def test_operation_adapter_encodes_existing_bus_contract(self):
        service = PostureService()
        fake = FakeService()
        service.call = fake.call
        service.set_mode('stand')
        service.set_preference('tent_enabled', False)
        service.set_preference('tent_prompt', True)
        service.set_preference('stand_prompt', False)
        service.confirm_prompt('tent', 7)
        service.confirm_prompt('stand', 8)
        service.calibrate()
        service.reset_calibration()
        self.assertEqual([call[:2] for call in fake.calls], [
            ('SetMode', ('stand',)), ('SetTentEnabled', (False,)),
            ('SetTentPrompt', (True,)), ('SetStandPrompt', (False,)),
            ('ConfirmTent', (7,)), ('ConfirmStand', (8,)),
            ('CalibrateSensor', None), ('ResetSensorCalibration', None)])

    def test_error_mapping_shared_and_details_hidden(self):
        self.assertEqual(set(ERROR_MESSAGES), set(ALL_ERROR_CODES))
        for code in ALL_ERROR_CODES:
            message = describe_error(Exception(f'GDBus.Error:work.mmoe.panasonic.Posture.Error: {code}: PRIVATE'))
            self.assertEqual(message, error_text(code))
            self.assertNotIn('PRIVATE', message)
        self.assertIn('unavailable', describe_error(ServiceUnavailable()))
        self.assertEqual(describe_error(Exception('AccessDenied: PRIVATE')), error_text('access-denied'))

    def test_parse_state_strict_and_compatible_defaults(self):
        raw = state()
        for field in ('tent_enabled', 'posture_pending', 'touch_wake'):
            raw.pop(field)
        decoded = parse_state(json.dumps(raw))
        self.assertTrue(decoded['tent_enabled'])
        self.assertFalse(decoded['posture_pending'])
        self.assertIsNone(decoded['touch_wake'])
        for change in ({'version': 999}, {'generation': True}, {'target': []},
                       {'posture_pending': 1}, {'applied': []}, {'manual': 'invalid'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_state(json.dumps(dict(raw, **change)))

    def test_retry_fallback_only_for_unknown_method(self):
        service = PostureService()
        fake = FakeService()
        service.call = fake.call
        result = service.retry('tent')
        fake.calls[0][2].set_exception(Exception('UnknownMethod'))
        self.assertEqual(fake.calls[1][:2], ('SetMode', ('tent',)))
        fake.calls[1][2].set_result(None)
        self.assertIsNone(result.result())
        result = service.retry('tent')
        fake.calls[-1][2].set_exception(Exception('AccessDenied'))
        self.assertIn('AccessDenied', str(result.exception()))
        self.assertEqual(len(fake.calls), 3)

    def test_old_proxy_refresh_action_and_signal_are_ignored(self):
        class Proxy:
            def __init__(self):
                self.pending = []
                self.disconnected = []
            def call(self, *args):
                self.pending.append(args[-1])
            def call_finish(self, result):
                if isinstance(result, BaseException):
                    raise result
                return GLib.Variant('(s)', (json.dumps(state()),))
            def disconnect(self, handler):
                self.disconnected.append(handler)
        old, new = Proxy(), Proxy()
        service = PostureService()
        states, unavailable = [], []
        service._on_state = states.append
        service._on_unavailable = lambda: unavailable.append(True)
        service._proxy = old
        service._handlers = [1, 2]
        service.refresh()
        action = service.call('SetMode')
        service._drop_proxy()
        self.assertEqual(old.disconnected, [1, 2])
        service._proxy = new
        old.pending[0](old, None)
        old.pending[1](old, None)
        self.assertIsInstance(action.exception(), ServiceUnavailable)
        service._signal(old, '', 'StateChanged', GLib.Variant('(s)', (json.dumps(state()),)))
        self.assertEqual(states, [])
        self.assertEqual(unavailable, [])
        service.refresh()
        new.pending[0](new, None)
        self.assertEqual(len(states), 1)
        service.stop()

    def test_replaced_owner_invalidates_connection_and_pending_actions(self):
        class Proxy:
            def get_name_owner(self):
                return ':1.2'
        service = PostureService()
        service._proxy = Proxy()
        service._owner = ':1.1'
        unavailable = []
        service._on_unavailable = lambda: unavailable.append(True)
        service._owner_changed(service._proxy)
        self.assertEqual(unavailable, [True])
        self.assertIsNone(service._proxy)
        self.assertTrue(service._retry_id)
        service.stop()
        self.assertFalse(service._retry_id)

    def test_malformed_signal_does_not_break_connection(self):
        service = PostureService()
        proxy = object()
        service._proxy = proxy
        with self.assertLogs('panasonic_posture.ui.service', level='ERROR'):
            service._signal(proxy, '', 'StateChanged', GLib.Variant('()', ()))
        self.assertIs(service._proxy, proxy)
        service.stop()

    def test_bad_state_and_render_error_do_not_drop_connection(self):
        service = PostureService()
        proxy = object()
        service._proxy = proxy
        service._on_state = lambda value: (_ for _ in ()).throw(RuntimeError('widget'))
        with self.assertLogs('panasonic_posture.ui.service', level='ERROR'):
            service._emit_state('[]')
            service._emit_state(json.dumps(state()))
        self.assertIs(service._proxy, proxy)
        service.stop()

    def test_per_uid_name_remains_valid(self):
        from gi.repository import Gio
        from panasonic_posture.linux_contract import session_name
        self.assertTrue(Gio.dbus_is_name(session_name(1000)))

    def test_python_entry_and_packaging(self):
        import inspect
        from panasonic_posture.cli import launch_ui
        self.assertNotIn('execv', inspect.getsource(launch_ui))
        for name in ('packaging/aur/PKGBUILD', 'packaging/aur/.SRCINFO',
                     'packaging/aur-bin/PKGBUILD.in', 'packaging/deb/control.in',
                     'packaging/rpm/panasonic-posture-manager.spec.in'):
            self.assertNotIn('gjs', (ROOT / name).read_text())
        self.assertFalse(list((ROOT / 'data/ui').glob('*.js')))


class WidgetTests(unittest.TestCase):
    """Run each scenario in a new process so GTK never uses the real display."""
    @classmethod
    def setUpClass(cls):
        broadway = shutil.which('gtk4-broadwayd')
        if not broadway:
            raise unittest.SkipTest('gtk4-broadwayd is required')
        cls.temp = tempfile.TemporaryDirectory()
        cls.env = dict(os.environ, GDK_BACKEND='broadway', GTK_A11Y='none',
                       XDG_RUNTIME_DIR=cls.temp.name, LANG='C', LANGUAGE='C',
                       GIO_USE_VFS='local', GSETTINGS_BACKEND='memory',
                       PYTHONPATH=str(ROOT / 'src') + ':' + str(ROOT / 'tests'))
        config = Path(cls.temp.name) / 'bus.conf'
        config.write_text('<busconfig><type>session</type><listen>unix:tmpdir=' + cls.temp.name + '</listen><auth>EXTERNAL</auth><policy context="default"><allow send_destination="*"/><allow own="*"/><allow eavesdrop="true"/></policy></busconfig>')
        cls.bus_config = config
        # Choose a private socket and port; never attach to an existing display.
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        number = port
        cls.env['BROADWAY_DISPLAY'] = f':{number}'
        cls.server = subprocess.Popen([broadway, '--port', str(port), f':{number}'], env=cls.env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        socket_path = Path(cls.temp.name) / f'broadway{number + 1}.socket'
        deadline = time.monotonic() + 5
        while not socket_path.exists() and time.monotonic() < deadline and cls.server.poll() is None:
            time.sleep(.02)
        if not socket_path.exists():
            cls.server.terminate()
            _, error = cls.server.communicate(timeout=5)
            cls.temp.cleanup()
            raise RuntimeError(f'Private Broadway failed: {error!r}')

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.communicate(timeout=5)
        cls.temp.cleanup()

    def run_ui(self, body):
        prelude = '''
from test_ui_contract import FakeService, state
from panasonic_posture.ui import app
from panasonic_posture.ui.posture import PosturePage
from panasonic_posture.ui.settings import SettingsPage
from gi.repository import Adw, Gtk, Gdk, GLib
Adw.init()
service = FakeService()
toasts = []
page = PosturePage(service, toasts.append)
settings = SettingsPage(service, toasts.append)
s = state()
page.update(s)
settings.update(s)
'''
        result = subprocess.run(['dbus-run-session', '--config-file', str(self.bus_config), '--', sys.executable, '-c', prelude + body],
                                env=self.env, text=True, capture_output=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertNotIn('CRITICAL', result.stderr)

    def test_pages_and_window_shell(self):
        self.run_ui('''
application = app.Application(application_id='work.mmoe.Posture.Test', service_factory=lambda: service)
assert application.register(None)
application.ensure_ui()
assert application.window.get_default_size() == (500, 660)
assert application.stack.get_visible_child_name() == 'posture'
class Command:
    def get_arguments(self): return ['test', '--settings']
application._command_line(application, Command())
assert application.stack.get_visible_child_name() == 'settings'
service.on_state(s)
assert application.settings_page.radios['auto'].get_active()
service.on_unavailable()
assert not application.settings_page.radios['auto'].get_sensitive()
loop = GLib.MainLoop()
GLib.timeout_add(150, lambda: (loop.quit(), False)[1])
loop.run()
application._shutdown(application)
assert service.stopped
page.close()
''')

    def test_real_second_launch_forwards_settings_option(self):
        self.run_ui('''
import subprocess, sys
application = app.Application(service_factory=lambda: service)
assert application.register(None)
application.ensure_ui()
remote = subprocess.Popen([sys.executable, '-m', 'panasonic_posture.cli', '--settings'],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
loop = GLib.MainLoop()
expired = []
def poll():
    if remote.poll() is not None:
        loop.quit()
        return False
    return True
def timeout():
    expired.append(True)
    remote.terminate()
    loop.quit()
    return False
poll_id = GLib.timeout_add(10, poll)
timeout_id = GLib.timeout_add(5000, timeout)
loop.run()
if expired:
    GLib.source_remove(poll_id)
else:
    GLib.source_remove(timeout_id)
output, errors = remote.communicate(timeout=5)
assert not expired, errors
assert remote.returncode == 0, errors
assert application.stack.get_visible_child_name() == 'settings'
application._shutdown(application)
page.close()
''')

    def test_displayed_pose_and_readiness(self):
        self.run_ui('''
for mode, expected in [('tent','Tent'),('stand','Stand'),('hardware','Tablet'),('laptop','Laptop')]:
    page.update(state(applied={'mode': mode}))
    assert page.heading.get_label() == expected
page.update(state(manual='tent', target={'mode':'tent'}, pose='laptop'))
assert page.heading.get_label() == 'Tent'
page.update(state(manual='tent', target={'mode':'tent'}, pose='laptop', error_code='internal-error'))
assert page.heading.get_label() == 'Laptop'
assert 'Mode change failed' in page.detail.get_label()
page.update(state(pose='unknown', target={'mode':'unavailable'}, hardware=True))
assert page.heading.get_label() == 'Laptop'
assert not page.detail.get_visible()
page.update(state(target={'mode':'unavailable'}, hardware=None))
assert 'tablet switch is unavailable' in page.detail.get_label()
page.update(state(target={'mode':'unavailable'}, sensor_source='starting'))
assert 'No posture data' in page.detail.get_label()
page.update(state(supported=False))
assert 'does not support' in page.detail.get_label()
page.update(state(support_tier='compatible'))
assert 'Compatibility mode' in page.detail.get_label()
page.update(state(session_connected=False))
assert 'desktop to get ready' in page.detail.get_label()
page.set_unavailable()
assert page.detail.get_label() == 'Background service unavailable'
page.close()
''')

    def test_transition_timer_and_generation_cleanup(self):
        self.run_ui('''
assert not page.detail.get_visible()
loop = GLib.MainLoop()
GLib.timeout_add(2150, lambda: (loop.quit(), False)[1])
loop.run()
assert 'longer than usual' in page.detail.get_label()
page.update(state(generation=2))
assert not page._slow
page.update(state(generation=2, applied={'mode':'laptop'}))
assert page._timer == 0
page.update(state(generation=3))
page.set_unavailable()
assert page._timer == 0
page.close()
''')

    def test_return_to_automatic_and_disconnect(self):
        self.run_ui('''
assert not page.auto_button.get_visible()
page.update(state(manual='tent', target={'mode':'tent'}))
assert page.auto_button.get_visible()
page.auto_button.emit('clicked')
assert service.calls[-1][:2] == ('SetMode', ('auto',))
assert not page.auto_button.get_sensitive()
page.auto_button.emit('clicked')
assert len(service.calls) == 1
service.calls[-1][2].set_exception(Exception('internal-error'))
assert page.auto_button.get_sensitive()
page.update(state(manual='auto'))
assert not page.auto_button.get_visible()
page.update(state(manual='stand'))
page.set_unavailable()
assert not page.auto_button.get_visible()
page.close()
''')

    def test_confirmation_dedup_and_current_prompt(self):
        self.run_ui('''
page.update(state(pending_notification=8, pending_prompt='tent'))
page.confirm_button.emit('clicked')
assert service.calls[-1][:2] == ('ConfirmTent', (8,))
assert not page.confirm_button.get_sensitive()
page.confirm_button.emit('clicked')
assert len(service.calls) == 1
page.update(state(pending_notification=9, pending_prompt='stand'))
service.calls[0][2].set_result(None)
assert page.confirm_button.get_label() == 'Enter stand mode'
assert page.confirm_button.get_sensitive()
page.retry_button.emit('clicked')
assert service.calls[-1][0] == 'Retry'
service.calls[-1][2].set_exception(Exception('internal-error: PRIVATE'))
assert 'PRIVATE' not in page.detail.get_label()
page.close()
''')

    def test_settings_coalesce_and_failure_resync(self):
        self.run_ui('''
settings.radios['tent'].set_active(True)
settings.radios['stand'].set_active(True)
assert len(service.calls) == 1
settings.update(s)
assert settings.radios['stand'].get_active()
service.calls[0][2].set_exception(Exception('old failure'))
assert service.calls[1][:2] == ('SetMode', ('stand',))
assert not toasts
service.calls[1][2].set_exception(Exception('internal-error'))
assert settings.radios['auto'].get_active()
assert len(toasts) == 1
assert not settings.switches['suspend_touch_wake'].get_sensitive()
assert 'No supported' in settings.switches['suspend_touch_wake'].get_subtitle()
settings.update(state(suspend_touch_wake_available=True))
for field, method in [('suspend_touch_wake','SetSuspendTouchWake'),('tent_enabled','SetTentEnabled'),('tent_prompt','SetTentPrompt'),('stand_prompt','SetStandPrompt')]:
    initial = settings.switches[field].get_active()
    settings.switches[field].set_active(not initial)
    settings.switches[field].set_active(initial)
    before = len(service.calls)
    assert service.calls[-1][0] == method
    service.calls[-1][2].set_result(None)
    assert len(service.calls) == before + 1
    service.calls[-1][2].set_result(None)
page.close()
''')

    def test_keyboard_and_hardware_safety(self):
        self.run_ui('''
window = Gtk.Window()
window.set_child(settings.widget)
window.present()
settings.radios['auto'].grab_focus()
assert settings.browse_modes(settings.radios['auto'], Gdk.KEY_Down)
assert settings.radios['auto'].get_active()
assert not service.calls
settings.update(state(hardware=True))
assert not any(b.get_sensitive() for b in settings.radios.values())
settings.update(state(tent_enabled=False))
assert settings.radios['tent'].get_sensitive()
assert not settings.switches['tent_prompt'].get_sensitive()
settings.set_unavailable()
assert not any(b.get_sensitive() for b in settings.radios.values())
page.close()
''')

    def test_calibration_and_stale_results(self):
        self.run_ui('''
settings._calibrate()
assert service.calls[-1][0] == 'CalibrateSensor'
assert not settings.calibrate_button.get_sensitive()
service.calls[-1][2].set_result(None)
assert toasts[-1] == 'Sensor direction calibrated.'
settings.update(state(sensor_calibrated=True))
assert settings.reset_button.get_visible()
settings._calibrate(True)
service.calls[-1][2].set_exception(Exception('calibration-not-ready: PRIVATE'))
assert 'PRIVATE' not in toasts[-1]
settings.radios['tent'].set_active(True)
old = service.calls[-1][2]
settings.set_unavailable()
settings.update(s)
settings.radios['stand'].set_active(True)
old.set_exception(Exception('old failure'))
assert settings.radios['stand'].get_active()
page.update(state(pending_notification=1))
page.confirm_button.emit('clicked')
old = service.calls[-1][2]
page.set_unavailable()
old.set_exception(Exception('old failure'))
assert page.detail.get_label() == 'Background service unavailable'
page.close()
''')

    def test_real_async_bus_connection_signal_retry_and_reconnect(self):
        self.run_ui('''
from gi.repository import Gio
from panasonic_posture.ui.service import PostureService
from panasonic_posture.linux_contract import NAME, PATH, IFACE, DBUS_XML
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
flags = Gio.DBusCallFlags.NONE
bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus',
              'RequestName', GLib.Variant('(su)', (NAME, 0)), None, flags, 1000, None)
methods = []
def invoke(connection, sender, path, interface, method, parameters, invocation):
    methods.append((method, parameters.unpack()))
    if method == 'GetState':
        import json
        invocation.return_value(GLib.Variant('(s)', (json.dumps(s),)))
    else:
        invocation.return_value(None)
registration = bus.register_object(PATH, Gio.DBusNodeInfo.new_for_xml(DBUS_XML).interfaces[0], invoke, None, None)
client = PostureService(bus)
states, unavailable = [], []
client.start(states.append, lambda: unavailable.append(True))
def wait_until(predicate):
    loop = GLib.MainLoop()
    expired = []
    def check():
        if predicate():
            loop.quit()
            return False
        return True
    def timeout():
        expired.append(True)
        loop.quit()
        return False
    check_id = GLib.timeout_add(10, check)
    timeout_id = GLib.timeout_add(5000, timeout)
    loop.run()
    if expired:
        GLib.source_remove(check_id)
    else:
        GLib.source_remove(timeout_id)
    assert not expired, 'async operation timed out'
wait_until(lambda: len(states) == 1)
result = client.call('SetTentEnabled', GLib.Variant('(b)', (False,)))
wait_until(result.done)
assert result.exception() is None
assert methods[-1] == ('SetTentEnabled', (False,))
import json
bus.emit_signal(None, PATH, IFACE, 'StateChanged', GLib.Variant('(s)', (json.dumps(state(generation=2)),)))
wait_until(lambda: len(states) == 2)
bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus',
              'ReleaseName', GLib.Variant('(s)', (NAME,)), None, flags, 1000, None)
wait_until(lambda: bool(unavailable))
assert client._proxy is None
bus.call_sync('org.freedesktop.DBus','/org/freedesktop/DBus','org.freedesktop.DBus',
              'RequestName', GLib.Variant('(su)', (NAME, 0)), None, flags, 1000, None)
wait_until(lambda: len(states) == 3)
client.stop()
bus.unregister_object(registration)
page.close()
''')

    def test_four_illustrations_all_themes_cache_and_missing_assets(self):
        self.run_ui('''
from panasonic_posture.ui.illustration import POSES, posture_pixbuf, _CACHE, CACHE_LIMIT
from panasonic_posture.ui.common import DATA_ROOT
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
assert POSES == {'laptop', 'stand', 'tent', 'tablet'}
assert not (DATA_ROOT / 'assets/unknown.svg').exists()
for pose in POSES:
    for dark in (False, True):
        for hc in (False, True):
            pixbuf = posture_pixbuf(DATA_ROOT, pose, dark, hc)
            assert (pixbuf.get_width(), pixbuf.get_height()) == (640, 480)
assert len(_CACHE) <= CACHE_LIMIT
assert posture_pixbuf(DATA_ROOT, '../bad') is None
assert posture_pixbuf(DATA_ROOT, 'unknown') is None
with tempfile.TemporaryDirectory() as directory:
    with patch('panasonic_posture.ui.posture.DATA_ROOT', Path(directory)):
        with unittest.TestCase().assertLogs('panasonic_posture.ui.posture', level='ERROR'):
            page.refresh_theme()
        assert page._pixbuf is None
page.close()
''')


if __name__ == '__main__':
    unittest.main()
