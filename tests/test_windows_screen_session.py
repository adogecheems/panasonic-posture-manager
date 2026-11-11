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

from panasonic_posture.model import (
    DeviceInfo, HardwareStatus, SensorInfo, SensorReading, StateSnapshot, Target,
)
from panasonic_posture.service.engine import Engine
from panasonic_posture.session.failures import DisplayError, ServiceError
from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.backend import Backend, BackendError
from panasonic_posture.windows.display import Panel
from panasonic_posture.windows.orientation import OrientationReading
from panasonic_posture.windows.protocol import validate_state, ProtocolError
from panasonic_posture.windows.screen_session import (
    SCREEN_MODES, ScreenSession, SessionBridge, ProfileSensor, available_screen_modes,
    select_native_display, verified_machine,
)
from panasonic_posture.windows.session_display import QualifiedDisplay
from panasonic_posture.windows.hardware_release import HardwareReleaseCoordinator
from panasonic_posture.windows.simulation import SimulatedDisplay


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
        self.backend = Backend(Path(self.directory.name) / 'screen.json', screen_factory=factory)
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
            self.assertNotIn('native_backend', state)
            self.assertFalse(state['input_control_available'])
            self.assertFalse(state['applied']['disable_internal_input'])
            self.assertEqual(self.display.current.transform, rotation)
        with self.assertRaises((BackendError, ValueError)):
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


class HardwareReleasePreparationTests(unittest.TestCase):
    def test_failed_preparation_restores_display_and_releases_slate(self):
        class FailingDisplay:
            def __init__(self):
                self.restore_calls = 0

            def prepare_hardware_release(self):
                raise OSError('readback failed')

            def restore(self):
                self.restore_calls += 1

        display = FailingDisplay()
        releases = []
        qualified = QualifiedDisplay(
            display,
            SimpleNamespace(control_available=lambda: True),
            SimpleNamespace(release=lambda: releases.append(True)),
        )
        with self.assertRaisesRegex(OSError, 'readback failed'):
            qualified.prepare_hardware_release()
        self.assertEqual(display.restore_calls, 1)
        self.assertEqual(releases, [True])

    def test_tent_pose_while_still_physically_folded_prepares_once(self):
        class Prepared:
            calls = 0
            def prepare_hardware_release(self):
                self.calls += 1

        session = object.__new__(ScreenSession)
        session.qualified = Prepared()
        session.hardware_release = HardwareReleaseCoordinator(session.qualified)
        session.engine = SimpleNamespace(state=StateSnapshot(
            instance='one', generation=3, hardware=True, pose='tablet',
            target=Target(mode='hardware'),
        ), hardware_release_pose='tent')
        session._prepare_hardware_release()
        session._prepare_hardware_release()
        self.assertEqual(session.qualified.calls, 2)

        session.engine.state.hardware = False
        session.engine.hardware_release_pose = None
        session._prepare_hardware_release()
        session.engine.state.hardware = True
        session.engine.hardware_release_pose = 'tent'
        session._prepare_hardware_release()
        self.assertEqual(session.qualified.calls, 3)


class BridgeTests(unittest.TestCase):
    def test_refreshable_display_does_not_advertise_manual_tablet(self):
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
        self.assertEqual(available_screen_modes(tablet), SCREEN_MODES)

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
            api, missing, hardware_query=None,
        )
        self.assertEqual(selected.backend_name, 'windows-external-only')
        self.assertEqual(selected.api, api)
        pending = SimpleNamespace(load=lambda: {'version': 1})
        with self.assertRaises(DisplayError) as caught:
            select_native_display(
                api, pending, hardware_query=None,
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

if __name__ == '__main__': unittest.main()
