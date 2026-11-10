"""Real shared screen pipeline with injected OS ports, never host hardware."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import os
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest

from panasonic_posture.model import DeviceInfo, HardwareStatus, SensorInfo, SensorReading, StateSnapshot
from panasonic_posture.service.engine import Engine
from panasonic_posture.session.failures import DisplayError, ServiceError
from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.backend import Backend, BackendError
from panasonic_posture.windows.display import Panel
from panasonic_posture.windows.orientation import OrientationReading, SIMPLE_ORIENTATION_TRANSFORM
from panasonic_posture.windows.protocol import validate_state, ProtocolError
from panasonic_posture.windows.screen_session import (
    SCREEN_MODES, ScreenSession, SessionBridge, ProfileSensor, available_screen_modes,
    select_native_display, verified_machine,
)
from panasonic_posture.windows.simulation import SimulatedDisplay
from panasonic_posture.windows.tablet_display import WindowsTabletDisplay


def screen_worker(path):
    """Subprocess fixture with real journals and a file-backed OS display."""
    from panasonic_posture.session.journal import Journal
    from panasonic_posture.session.transaction import SoftwareDisplay, DisplayState
    from panasonic_posture.windows.__main__ import serve
    path = Path(path)
    rotation = path.with_suffix('.rotation')
    class FileDisplay(SoftwareDisplay):
        backend_name = 'windows'
        def _read_state(self):
            return DisplayState(('fixture-panel',), int(rotation.read_text()) if rotation.exists() else 0)
        def _write_rotation(self, state, transform):
            rotation.write_text(str(transform))
    def factory(store, instance, journal):
        return ScreenSession(store=store, instance=instance,
            sensor=SimpleNamespace(info=SensorInfo(ready=True, backend='windows-winrt'),
                reading=lambda: SensorReading((0., 0., -9.81), time.monotonic()),
                start=lambda: None, close=lambda: None),
            switch=SimpleNamespace(query=lambda: (False, HardwareStatus.OK)),
            display=FileDisplay(Journal(journal)), context=Context(), device=DeviceInfo(platform='fixture'))
    return serve(sys.stdin.buffer, sys.stdout.buffer, Backend(path, native=True, screen_factory=factory))


class Clock:
    def __init__(self): self.now = time.monotonic()
    def __call__(self): return self.now


class Context:
    available = True
    closed = False
    screen_off = False
    def poll(self): pass
    def control_available(self): return self.available
    def unattended(self): return self.screen_off or not self.available
    def close(self): self.closed = True


class NativeScreenTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock, self.context = Clock(), Context()
        self.hardware = False
        self.vector = (0., 0., -9.81)
        self.display = SimulatedDisplay()
        self.sensor = SimpleNamespace(info=SensorInfo(ready=True, backend='windows-winrt'),
            reading=lambda: SensorReading(self.vector, self.clock()), start=lambda: None, close=lambda: None)
        def factory(store, instance, journal):
            return ScreenSession(store=store, instance=instance, sensor=self.sensor,
                switch=SimpleNamespace(query=lambda: (self.hardware, HardwareStatus.OK)),
                display=self.display, context=self.context, clock=self.clock, device=DeviceInfo(platform='windows'))
        self.backend = Backend(Path(self.directory.name) / 'native.json', native=True, screen_factory=factory)
        self.addCleanup(self.backend.close)
        self.advance(85)

    def advance(self, count=30):
        for _ in range(count):
            self.clock.now += .05
            self.backend.advance(self.clock.now)
            time.sleep(.001)

    def until(self, condition):
        for _ in range(300):
            self.advance(1)
            if condition(): return
        self.fail('screen pipeline did not settle')

    def test_real_engine_modes_have_truthful_capabilities_and_display_receipts(self):
        self.until(lambda: self.backend.snapshot()['applied'] is not None)
        self.assertEqual(self.backend.snapshot()['available_modes'],
                         ['auto', 'laptop', 'tent', 'stand'])
        for mode, rotation in (('tent', 2), ('stand', 0), ('laptop', 0)):
            self.backend.call('set_mode', {'mode': mode})
            self.assertIsNone(self.backend.snapshot()['applied'])
            self.until(lambda: (self.backend.snapshot()['applied'] or {}).get('mode') == mode)
            state = self.backend.snapshot()
            self.assertEqual(validate_state(state, native=True), state)
            self.assertFalse(state['input_control_available'])
            self.assertFalse(state['applied']['disable_internal_input'])
            self.assertEqual(self.display.current.transform, rotation)
        with self.assertRaises(BackendError):
            self.backend.call('set_mode', {'mode': 'tablet'})

    def test_display_failure_never_publishes_success_and_retry_uses_original_runtime(self):
        self.display.fail = True
        self.backend.call('set_mode', {'mode': 'tent'})
        self.until(lambda: bool(self.backend.snapshot()['error_code']))
        self.assertIsNone(self.backend.snapshot()['applied'])
        self.display.fail = False
        self.backend.call('retry', {'fallback_mode': None})
        self.until(lambda: (self.backend.snapshot()['applied'] or {}).get('mode') == 'tent')

    def test_qualification_loss_revokes_lease_and_restores_and_can_resume(self):
        self.backend.call('set_mode', {'mode': 'tent'})
        self.until(lambda: (self.backend.snapshot()['applied'] or {}).get('mode') == 'tent')
        self.context.available = False
        self.advance()
        self.assertIsNone(self.backend.snapshot()['applied'])
        self.until(lambda: self.display.current.transform == 0)
        self.context.available = True
        self.until(lambda: (self.backend.snapshot()['applied'] or {}).get('mode') == 'tent')

    def test_stale_sensor_restores_and_physical_switch_still_wins(self):
        self.backend.call('set_mode', {'mode': 'tent'})
        self.until(lambda: (self.backend.snapshot()['applied'] or {}).get('mode') == 'tent')
        self.sensor.info = replace(self.sensor.info, ready=False, error='lost')
        self.sensor.reading = lambda: SensorReading(source='windows-winrt')
        self.until(lambda: self.display.current.transform == 0)
        self.assertIsNone(self.backend.snapshot()['applied'])
        self.hardware = True
        self.until(lambda: (self.backend.snapshot()['applied'] or {}).get('mode') == 'hardware')
        self.assertIs(self.backend.snapshot()['hardware'], True)
        self.assertEqual(self.backend.snapshot()['manual'], 'auto')

    def test_preferences_calibration_and_close_restore(self):
        self.backend.call('set_preference', {'field': 'tent_enabled', 'enabled': False})
        self.assertFalse(self.backend.snapshot()['tent_enabled'])
        self.backend.call('calibrate', {})
        self.assertTrue(self.backend.snapshot()['sensor_calibrated'])
        self.backend.call('reset_calibration', {})
        self.assertFalse(self.backend.snapshot()['sensor_calibrated'])
        self.backend.call('set_mode', {'mode': 'tent'})
        self.until(lambda: self.display.current.transform == 2)
        self.backend.close()
        self.assertEqual(self.display.current.transform, 0)
        self.assertTrue(self.context.closed)
        self.assertIsNone(self.backend.screen.engine.session_owner)
        self.backend.close()

    def test_native_snapshot_requires_explicit_client_and_rejects_input_claims(self):
        state = self.backend.snapshot()
        with self.assertRaises(ProtocolError): validate_state(state)
        for changes in ({'input_control_available': True}, {'native_backend': False},
                        {'simulation': True}, {'available_modes': ['auto', 'tablet']},
                        {'support_tier': 'validated'},
                        {'target': dict(state['target'], disable_internal_input=True)}):
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                validate_state(dict(state, **changes), native=True)

    def test_known_screen_off_uses_shared_direct_posture_policy_without_losing_qualification(self):
        self.context.screen_off = True
        self.advance(30)
        self.vector = (0., -9.81, 0.)
        self.until(lambda: (self.backend.snapshot()['applied'] or {}).get('mode') == 'tent')
        self.assertIsNone(self.backend.snapshot()['pending_notification'])
        self.assertTrue(self.context.control_available())
        self.context.screen_off = False
        self.advance(30)
        self.assertEqual((self.backend.snapshot()['applied'] or {}).get('mode'), 'tent')
        self.assertIsNone(self.backend.snapshot()['pending_notification'])
        self.assertTrue(self.context.control_available())


class TabletApi:
    def __init__(self):
        self.current = Panel('panel', 'source', (1, 0, 0), 11, 0, 2880, 1920, 1)

    def read(self):
        return self.current

    def lock(self, locked):
        self.current = replace(self.current, auto_rotation=1 if locked else 0)

    def rotate(self, identity, previous, transform):
        if (identity, previous) != (self.current.identity, self.current.transform):
            raise AssertionError('display identity changed')
        self.current = replace(self.current, transform=transform)


class SoftwareTabletScreenTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.clock, self.context = Clock(), Context()
        self.api = TabletApi()
        self.value, self.missing, self.hardware = 4, False, False
        self.sensor = SimpleNamespace(
            info=SensorInfo(ready=True, backend='windows-winrt'),
            reading=lambda: SensorReading((0., 0., -9.81), self.clock()),
            orientation=lambda: None if self.missing else OrientationReading(
                self.value, self.clock(), 'orientation-1'),
            start=lambda: None, close=lambda: None,
        )

        def factory(store, instance, journal):
            display = WindowsTabletDisplay(
                journal=Journal(journal), api=self.api, clock=self.clock,
                orientation=self.sensor.orientation,
                mapping=SIMPLE_ORIENTATION_TRANSFORM,
                hardware_query=lambda: (self.hardware, HardwareStatus.OK),
            )
            return ScreenSession(store=store, instance=instance, sensor=self.sensor,
                switch=SimpleNamespace(query=lambda: (self.hardware, HardwareStatus.OK)),
                display=display, context=self.context, clock=self.clock,
                device=DeviceInfo(platform='windows'))

        self.backend = Backend(Path(self.directory.name) / 'tablet.json',
                               native=True, screen_factory=factory)
        self.addCleanup(self.backend.close)
        self.advance(85)

    def advance(self, count=30):
        for _ in range(count):
            self.clock.now += .05
            self.backend.advance(self.clock.now)
            time.sleep(.001)

    def until(self, condition):
        for _ in range(400):
            self.advance(1)
            if condition():
                return
        self.fail('software-tablet pipeline did not settle')

    def applied(self):
        return (self.backend.snapshot()['applied'] or {}).get('mode')

    def test_mode_follows_four_quadrants_and_restores_after_qualification_loss(self):
        self.until(lambda: self.applied() == 'laptop')
        state = self.backend.snapshot()
        self.assertEqual(state['available_modes'],
                         ['auto', 'laptop', 'tent', 'stand', 'tablet'])
        self.assertEqual(validate_state(state, native=True), state)

        self.backend.call('set_mode', {'mode': 'tablet'})
        self.until(lambda: self.applied() == 'tablet')
        self.assertEqual(self.api.current.transform, 0)
        for value, transform in ((1, 3), (2, 2), (3, 1), (0, 0)):
            self.value = value
            self.until(lambda transform=transform: self.api.current.transform == transform)
            self.until(lambda: self.applied() == 'tablet')

        self.context.available = False
        self.advance()
        self.assertIsNone(self.backend.snapshot()['applied'])
        self.until(lambda: self.api.current.transform == 0)
        self.assertIsNone(self.backend.screen.engine.state.applied)

    def test_missing_direction_and_physical_switch_never_confirm_stale_rotation(self):
        self.until(lambda: self.applied() == 'laptop')
        self.backend.call('set_mode', {'mode': 'tablet'})
        self.until(lambda: self.applied() == 'tablet')
        self.value = 1
        self.until(lambda: self.api.current.transform == 3)

        self.missing = True
        self.until(lambda: self.backend.snapshot()['error_code'])
        self.assertIsNone(self.backend.snapshot()['applied'])
        self.until(lambda: self.api.current.transform == 0)

        self.missing = False
        self.hardware = True
        self.until(lambda: self.applied() == 'hardware')
        self.assertEqual(self.api.current.transform, 0)
        self.assertIs(self.backend.snapshot()['hardware'], True)


class BridgeTests(unittest.TestCase):
    def test_refreshable_read_only_display_does_not_advertise_software_tablet(self):
        refreshable = SimpleNamespace(
            needs_refresh=lambda target: True,
            refresh=lambda state: None,
            supports_software_tablet=False,
        )
        self.assertEqual(available_screen_modes(refreshable), SCREEN_MODES)
        tablet = SimpleNamespace(
            needs_refresh=lambda target: True,
            refresh=lambda state: None,
        )
        self.assertEqual(available_screen_modes(tablet), SCREEN_MODES + ('tablet',))

    def test_native_machine_preflight_requires_the_collected_exact_model(self):
        from unittest.mock import patch

        class Key:
            def __enter__(self): return self
            def __exit__(self, *args): return False

        def identity(vendor, product):
            def query(key, name):
                del key
                return (vendor if name == 'SystemManufacturer' else product), 1
            return query

        with patch('winreg.OpenKey', return_value=Key()):
            with patch('winreg.QueryValueEx', side_effect=identity('Panasonic Corporation', 'CFQV8-1L')):
                self.assertEqual(verified_machine(), ('Panasonic Corporation', 'CFQV8-1L'))
            for vendor, product in (('Panasonic Corporation', 'CFQV9-1L'),
                                    ('Other', 'CFQV8-1L')):
                with self.subTest(vendor=vendor, product=product), \
                        patch('winreg.QueryValueEx', side_effect=identity(vendor, product)), \
                        self.assertRaises(OSError):
                    verified_machine()

    def test_native_display_selection_uses_read_only_external_layout_or_keeps_recovery(self):
        external = Panel('external', r'\\.\DISPLAY2', (1, 0, 1), 5, 0, 1920, 1080, 0)
        api = SimpleNamespace(panels=lambda: [external])
        missing = SimpleNamespace(load=lambda: None)
        selected = select_native_display(
            api, missing, orientation=lambda: None, hardware_query=None,
        )
        self.assertEqual(selected.backend_name, 'windows-external-only')
        self.assertEqual(selected.api, api)
        pending = SimpleNamespace(load=lambda: {'version': 1})
        with self.assertRaises(DisplayError) as caught:
            select_native_display(
                api, pending, orientation=lambda: None, hardware_query=None,
            )
        self.assertEqual(caught.exception.detail, 'external-only-display-recovery-pending')

    def test_close_rejects_receipt_accepted_before_snapshot_publication(self):
        bridge = SessionBridge(StateSnapshot(), lambda: True)
        engine = SimpleNamespace(session_update=lambda *args, **kwargs: True)
        with ThreadPoolExecutor(max_workers=1) as worker:
            request = worker.submit(bridge.session_update, 0, False)
            deadline = time.monotonic() + 2
            while bridge._requests.empty() and time.monotonic() < deadline: time.sleep(.001)
            bridge.drain(engine, 'owner-fixture')
            bridge.close()
            with self.assertRaises(ServiceError): request.result(timeout=2)
        bridge.publish(StateSnapshot(session_connected=True))
        self.assertEqual(bridge._accepted, [])

    def test_successful_receipt_is_not_returned_before_the_updated_snapshot_is_published(self):
        bridge = SessionBridge(StateSnapshot(session_connected=False), lambda: True)
        engine = SimpleNamespace(session_update=lambda *args, **kwargs: True)
        with ThreadPoolExecutor(max_workers=1) as worker:
            def receipt_then_read():
                bridge.session_update(0, False)
                return bridge.get_state().session_connected
            request = worker.submit(receipt_then_read)
            deadline = time.monotonic() + 2
            while bridge._requests.empty() and time.monotonic() < deadline: time.sleep(.001)
            internal = bridge._requests.queue[0][0]
            bridge.drain(engine, 'owner-fixture')
            self.assertFalse(internal.done(), 'success must follow the accepted-state publication')
            bridge.publish(StateSnapshot(session_connected=True))
            self.assertTrue(request.result(timeout=2))
        bridge.close()

    def test_native_startup_role_rejects_other_sessions_without_requiring_unlock(self):
        from unittest.mock import patch
        from panasonic_posture.windows.screen_session import require_native_user
        security = SimpleNamespace(require_unprivileged=lambda: None,
            process_identity=lambda pid: SimpleNamespace(session_id=1),
            kernel=SimpleNamespace(WTSGetActiveConsoleSessionId=lambda: 1))
        with patch('panasonic_posture.windows.security.WindowsSecurity', return_value=security):
            require_native_user()
            for console in (0, 0xffffffff, 2):
                security.kernel.WTSGetActiveConsoleSessionId = lambda: console
                with self.assertRaises(PermissionError): require_native_user()

    def test_different_settings_cannot_bypass_native_controller_lock_and_failed_start_releases_it(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = SimpleNamespace(close=lambda: None)
            with patch.dict(os.environ, LOCALAPPDATA=str(root)):
                with patch('panasonic_posture.windows.screen_session.require_native_user'), patch(
                        'panasonic_posture.windows.screen_session.native_screen_session', return_value=fake) as factory:
                    first = Backend(root / 'one.json', native=True)
                    try:
                        with self.assertRaises(OSError): Backend(root / 'two.json', native=True)
                        self.assertEqual(factory.call_count, 1)
                    finally:
                        first.close()
                    factory.side_effect = OSError('startup failed')
                    with self.assertRaises(OSError): Backend(root / 'two.json', native=True)
                    factory.side_effect = None
                    restarted = Backend(root / 'two.json', native=True)
                    restarted.close()

    def test_elevated_native_entry_is_rejected_before_settings_or_lock_files_are_touched(self):
        from unittest.mock import patch
        from panasonic_posture.windows.__main__ import main
        with patch('panasonic_posture.windows.screen_session.require_native_user', side_effect=PermissionError('elevated')):
            with patch('panasonic_posture.windows.backend.SettingsStore') as store:
                with self.assertRaises(PermissionError): Backend(Path('untouched.json'), native=True)
                store.assert_not_called()
            with patch('panasonic_posture.windows.__main__.sys.platform', 'win32'), patch(
                    'panasonic_posture.windows.__main__.InstanceLock') as lock:
                with self.assertLogs(level='ERROR'):
                    self.assertEqual(main(['--stdio', '--native', '--settings', 'untouched.json']), 1)
                lock.assert_not_called()

    def test_screen_core_does_not_import_the_privileged_host_input_or_session_transport(self):
        result = subprocess.run([sys.executable, '-S', '-c', '''
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name.startswith('panasonic_posture.windows.') and name.rsplit('.', 1)[-1] in (
            'actuator', 'hardware_host', 'hardware_ipc', 'session_client', 'session_agent', 'session_launch'):
            raise AssertionError(name)
sys.meta_path.insert(0, Block())
from panasonic_posture.windows.screen_session import ScreenSession
from panasonic_posture.windows.backend import Backend
'''], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bridge_only_mutates_engine_when_owner_drains_and_discards_cancelled_requests(self):
        calls = []
        bridge = SessionBridge(StateSnapshot(), lambda: True)
        engine = SimpleNamespace(session_update=lambda *args, **kw: calls.append(args) or True)
        with ThreadPoolExecutor(max_workers=1) as worker:
            request = worker.submit(bridge.session_update, 3, True)
            deadline = time.monotonic() + 2
            while bridge._requests.empty() and time.monotonic() < deadline: time.sleep(.001)
            self.assertEqual(calls, [])
            bridge.drain(engine, 'owner-fixture')
            bridge.publish(StateSnapshot())
            request.result(timeout=2)
            expired = worker.submit(bridge.session_update, 4, True)
            with self.assertRaises(ServiceError): expired.result(timeout=2)
            bridge.drain(engine, 'owner-fixture')
            self.assertEqual(len(calls), 1)
            pending = worker.submit(bridge.session_update, 5, True)
            while bridge._requests.empty() and time.monotonic() < deadline + 2: time.sleep(.001)
            bridge.close()
            with self.assertRaises(ServiceError): pending.result(timeout=2)
        with self.assertRaises(ServiceError): bridge.get_state()

    def test_profile_does_not_apply_mount_to_another_device(self):
        direction = OrientationReading(1, 10., 'orientation-1')
        sensor = SimpleNamespace(info=SensorInfo(ready=True, backend='windows-winrt', device='other'),
                                 reading=lambda: SensorReading((0., 0., -9.81), 10.),
                                 orientation=lambda: direction)
        port = ProfileSensor(sensor)
        self.assertFalse(port.info.ready)
        self.assertFalse(port.reading().valid)
        self.assertIsNone(port.orientation())
        sensor.info = replace(sensor.info, device='')
        self.assertFalse(port.info.ready)
        sensor.info = replace(sensor.info, device=r'\\?\HID#Vid_8087&Pid_0AC2#unique')
        self.assertTrue(port.info.ready)
        self.assertTrue(port.reading().valid)
        self.assertIs(port.orientation(), direction)

    def test_native_factory_wires_verified_orientation_and_tablet_display(self):
        from unittest.mock import patch
        import panasonic_posture.windows.screen_session as module
        captured = {}

        class Key:
            def __enter__(self): return self
            def __exit__(self, *args): return False

        def query(key, name):
            del key
            return (( 'Panasonic Corporation' if name == 'SystemManufacturer' else 'CFQV8-1L'), 1)

        sensor = SimpleNamespace(
            info=SensorInfo(ready=True, backend='windows-winrt'),
            reading=lambda: SensorReading((0., 0., -9.81), 10.),
            orientation=lambda: OrientationReading(0, 10., 'orientation-1'),
            start=lambda: None, close=lambda: None,
        )
        context = SimpleNamespace(control_available=lambda: True, close=lambda: None)
        switch = SimpleNamespace(query=lambda: (False, HardwareStatus.OK))
        slate = SimpleNamespace(close=lambda: None)
        native_api = SimpleNamespace(panels=lambda: [
            Panel('panel-1', r'\\.\DISPLAY1', (1, 0, 0), 11, 0, 2880, 1920, 1),
        ])

        class Sensor:
            def __init__(self, **kwargs):
                captured['sensor'] = kwargs
                self.info, self.reading = sensor.info, sensor.reading
                self.orientation, self.start, self.close = sensor.orientation, sensor.start, sensor.close

        class Display:
            def __init__(self, **kwargs):
                captured['display'] = kwargs

        class Session:
            def __init__(self, **kwargs):
                captured['session'] = kwargs

        with tempfile.TemporaryDirectory() as directory, \
                patch.object(module, 'require_native_user'), \
                patch('winreg.OpenKey', return_value=Key()), \
                patch('winreg.QueryValueEx', side_effect=query), \
                patch('panasonic_posture.windows.session_context.WindowsSessionContext', return_value=context), \
                patch('panasonic_posture.windows.switch.WindowsSwitch', return_value=switch), \
                patch('panasonic_posture.windows.convertible_slate.NativeSlateDevice', return_value=object()), \
                patch('panasonic_posture.windows.convertible_slate.SlateNotifier', return_value=slate), \
                patch('panasonic_posture.windows.slate_journal.SlateJournal', return_value=object()), \
                patch('panasonic_posture.windows.slate_journal.lease_path',
                      return_value=Path(directory) / 'slate.json'), \
                patch('panasonic_posture.windows.display.NativeDisplay', return_value=native_api), \
                patch('panasonic_posture.windows.sensor.WindowsSensor', Sensor), \
                patch('panasonic_posture.windows.tablet_display.WindowsTabletDisplay', Display), \
                patch.object(module, 'ScreenSession', Session):
            result = module.native_screen_session(object(), 'fixture', Path(directory) / 'display.json')

        self.assertIsInstance(result, Session)
        self.assertTrue(captured['sensor']['include_orientation'])
        self.assertEqual(captured['display']['mapping'], SIMPLE_ORIENTATION_TRANSFORM)
        self.assertTrue(callable(captured['display']['orientation']))
        self.assertIs(captured['display']['api'], native_api)
        self.assertIsInstance(captured['session']['sensor'], ProfileSensor)
        self.assertIsInstance(captured['session']['display'], Display)


class PrivateScreenProcessTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from panasonic_posture.windows.client import ProcessClient
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'settings.json'
        self.states = []
        real_popen = subprocess.Popen
        def spawn(command, **kwargs):
            self.assertIn('--native', command)
            bootstrap = ('import sys;sys.path.insert(0,sys.argv[1]);'
                         'from test_windows_screen_session import screen_worker;'
                         'raise SystemExit(screen_worker(sys.argv[2]))')
            return real_popen([sys.executable, '-c', bootstrap, str(Path(__file__).parent), str(self.path)], **kwargs)
        patches = patch('panasonic_posture.windows.client.subprocess.Popen', side_effect=spawn)
        patches.start()
        self.addCleanup(patches.stop)
        self.client = ProcessClient(self.path, native=True)
        self.addCleanup(self.client.stop)
        self.client.start(self.states.append, lambda: None)

    def until(self, predicate):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            self.client.poll()
            if predicate(): return
            time.sleep(.01)
        self.fail('private native fixture failed to reach state')

    def tent(self):
        self.until(lambda: bool(self.states and self.states[-1]['applied']))
        future = self.client.set_mode('tent')
        self.until(future.done)
        future.result()
        self.until(lambda: (self.states[-1]['applied'] or {}).get('mode') == 'tent')
        self.assertEqual(self.path.with_suffix('.rotation').read_text(), '2')

    def test_normal_stop_restores_before_returning_and_exits_successfully(self):
        self.tent()
        process = self.client.process
        self.client.stop()
        self.assertEqual(process.returncode, 0)
        self.assertEqual(self.path.with_suffix('.rotation').read_text(), '0')
        self.assertFalse(self.path.with_suffix('.display.json').exists())

    def test_forced_exit_recovers_journal_before_new_transaction_and_does_not_replay_mode(self):
        self.tent()
        instance = self.states[-1]['instance']
        self.client.process.kill()
        self.client.process.wait(timeout=3)
        self.until(lambda: bool(self.states and self.states[-1]['instance'] != instance and
                               (self.states[-1]['applied'] or {}).get('mode') == 'laptop'))
        self.assertEqual(self.path.with_suffix('.rotation').read_text(), '0')
        self.assertEqual(self.states[-1]['manual'], 'auto')


if __name__ == '__main__': unittest.main()
