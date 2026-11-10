"""Windows software backend, private IPC and persistence without host devices."""
import queue
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from panasonic_posture.windows.backend import Backend
from panasonic_posture.windows.client import ProcessClient
from panasonic_posture.windows.convertible_slate import SlateError
from panasonic_posture.windows.diagnostics import log_path
from panasonic_posture.windows.protocol import (
    STARTUP_REASONS, encode, encode_startup_failure, decode, validate_state, ProtocolError,
)
from panasonic_posture.windows.instance import InstanceLock
from panasonic_posture.windows.__main__ import startup_failure_reason


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'settings.json'
        self.backend = Backend(self.path)

    def test_unadapted_hardware_is_never_reported_as_applied(self):
        state = self.backend.snapshot()
        self.assertFalse(state['supported'])
        self.assertIsNone(state['hardware'])
        self.assertIsNone(state['applied'])
        self.assertTrue(state['preferences_available'])
        for method, params in [('set_mode', {'mode': 'tent'}), ('calibrate', {}),
                               ('confirm_prompt', {'prompt': 'tent', 'epoch': 1})]:
            with self.assertRaisesRegex(Exception, 'unsupported-model'):
                self.backend.call(method, params)
        self.assertFalse(self.path.exists())

    def test_preferences_survive_restart_without_persisting_manual_mode(self):
        self.backend.call('set_preference', {'field': 'tent_enabled', 'enabled': False})
        state = Backend(self.path).snapshot()
        self.assertFalse(state['tent_enabled'])
        self.assertEqual(state['manual'], 'auto')
        self.assertIsNone(state['applied'])

    def test_bad_arguments_and_failed_save_do_not_change_state(self):
        original = self.backend.snapshot()
        for params in ({'field': 'manual', 'enabled': True},
                       {'field': 'tent_prompt', 'enabled': 1},
                       {'field': 'tent_prompt', 'enabled': True, 'extra': True}):
            with self.assertRaises(ValueError):
                self.backend.call('set_preference', params)
        with patch.object(self.backend.store, 'save', side_effect=OSError('full')):
            with self.assertRaisesRegex(Exception, 'config-write-failed'):
                self.backend.call('set_preference', {'field': 'tent_prompt', 'enabled': False})
        self.assertEqual(self.backend.snapshot(), original)

    def test_simulation_is_explicit_and_uses_separate_settings(self):
        self.backend.call('set_preference', {'field': 'tent_prompt', 'enabled': False})
        simulated = Backend(self.path.with_name('simulation.json'), simulate=True)
        self.addCleanup(simulated.close)
        self.assertTrue(simulated.snapshot()['tent_prompt'])
        simulated.call('set_mode', {'mode': 'tent'})
        self.assertIsNone(simulated.snapshot()['applied'])
        simulated.advance(time.monotonic() + 1)
        self.assertEqual(simulated.snapshot()['applied']['mode'], 'tent')
        self.assertIsNone(Backend(self.path).snapshot()['applied'])

    def test_corruption_is_quarantined(self):
        self.path.write_bytes(b'\xff')
        self.assertTrue(Backend(self.path).snapshot()['tent_enabled'])
        self.assertEqual(len(list(self.path.parent.glob('settings.json.corrupt-*'))), 1)

    def simulation(self, scenario='laptop'):
        backend = Backend(self.path.with_name('simulation.json'), simulate=True, scenario=scenario)
        self.addCleanup(backend.close)
        return backend

    def test_engine_requires_display_and_input_receipts_and_revokes_on_lease_loss(self):
        backend = self.simulation()
        backend.call('set_mode', {'mode': 'tent'})
        self.assertIsNone(backend.snapshot()['applied'])
        self.assertFalse(backend.simulation.actuator.running)
        backend.advance(time.monotonic() + .5)
        self.assertEqual(backend.snapshot()['applied']['mode'], 'tent')
        self.assertEqual(backend.simulation.display.current.transform, 2)
        self.assertTrue(backend.simulation.actuator.running)
        backend.simulation.now += 4
        backend.simulation.engine.tick()
        self.assertIsNone(backend.snapshot()['applied'])
        self.assertFalse(backend.simulation.actuator.running)

    def test_display_failure_prevents_input_takeover_and_can_retry(self):
        backend = self.simulation()
        backend.simulation.display.fail = True
        backend.call('set_mode', {'mode': 'tent'})
        backend.advance(time.monotonic() + .5)
        self.assertIsNone(backend.snapshot()['applied'])
        self.assertFalse(backend.simulation.actuator.running)
        self.assertIsNotNone(backend.snapshot()['error_code'])
        backend.call('retry', {'fallback_mode': 'tent'})
        backend.advance(time.monotonic() + 1)
        self.assertEqual(backend.snapshot()['applied']['mode'], 'tent')

    def test_hardware_priority_and_calibration_failure_do_not_persist(self):
        backend = self.simulation('hardware')
        with self.assertRaisesRegex(Exception, 'hardware-priority'):
            backend.call('set_mode', {'mode': 'tent'})
        with self.assertRaisesRegex(Exception, 'calibration-not-ready'):
            backend.call('calibrate', {})
        self.assertFalse(backend.store.path.exists())

    def test_prompt_confirmation_requires_current_epoch(self):
        backend = self.simulation('prompt')
        state = backend.snapshot()
        self.assertEqual(state['pending_prompt'], 'tent')
        with self.assertRaisesRegex(Exception, 'expired-confirmation'):
            backend.call('confirm_prompt', {'prompt': 'tent', 'epoch': state['pending_notification'] + 1})
        backend.call('confirm_prompt', {'prompt': 'tent', 'epoch': state['pending_notification']})
        backend.advance(time.monotonic() + .5)
        self.assertEqual(backend.snapshot()['applied']['mode'], 'tent')

    def test_failed_simulated_preference_save_keeps_engine_state(self):
        backend = self.simulation()
        with patch.object(backend.store, 'save', side_effect=OSError('full')):
            with self.assertRaisesRegex(Exception, 'config-write-failed'):
                backend.call('set_preference', {'field': 'tent_prompt', 'enabled': False})
        self.assertTrue(backend.snapshot()['tent_prompt'])

    def test_calibration_round_trip_uses_existing_settings_schema(self):
        backend = self.simulation()
        backend.call('calibrate', {})
        self.assertTrue(backend.snapshot()['sensor_calibrated'])
        restarted = self.simulation()
        self.assertTrue(restarted.snapshot()['sensor_calibrated'])
        restarted.call('reset_calibration', {})
        self.assertFalse(restarted.snapshot()['sensor_calibrated'])

    def test_settings_lock_rejects_competing_host_and_releases(self):
        with InstanceLock(self.path):
            with self.assertRaises(OSError):
                with InstanceLock(self.path):
                    self.fail('competing instance acquired the same settings')
        with InstanceLock(self.path):
            pass


class ProtocolTests(unittest.TestCase):
    def test_startup_failure_frames_are_bounded_to_known_safe_reasons(self):
        for reason in STARTUP_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(decode(encode_startup_failure(reason)),
                                 {'version': 1, 'type': 'fatal', 'reason': reason})
        with self.assertRaises(ProtocolError):
            encode_startup_failure('private-detail')

    def test_backend_startup_errors_map_to_stable_user_safety_reasons(self):
        self.assertEqual(startup_failure_reason(
            SlateError('configure LaptopSlateExclusiveAccess=0 and restart Windows'), native=True),
            'slate-configuration-required')
        self.assertEqual(startup_failure_reason(
            PermissionError('remote session'), native=True), 'native-session-unavailable')
        self.assertEqual(startup_failure_reason(
            OSError('fixture'), native=True), 'native-startup-failed')
        self.assertEqual(startup_failure_reason(
            OSError('fixture'), native=False), 'backend-startup-failed')

    def test_windows_host_imports_without_linux_or_gui_dependencies(self):
        result = subprocess.run([sys.executable, '-S', '-c', '''
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name == 'gi' or name.startswith(('PyQt', 'qfluent')) or name.endswith('linux_contract'):
            raise AssertionError(name)
sys.meta_path.insert(0, Block())
from pathlib import Path
from tempfile import TemporaryDirectory
from panasonic_posture.windows.backend import Backend
with TemporaryDirectory() as directory:
    backend = Backend(Path(directory)/'settings.json', simulate=True)
    backend.call('set_mode', {'mode': 'tent'})
    backend.close()
'''], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_bounded_strict_frames(self):
        value = {'version': 1, 'type': 'request', 'id': 1, 'method': 'get_state', 'params': {}}
        self.assertEqual(decode(encode(value)), value)
        for frame in (b'{}', b'[]\n', b'{"version":true}\n', b'x' * 65537,
                      b'{"version":1,"version":1}\n', b'{"version":1,"x":NaN}\n'):
            with self.assertRaises(ProtocolError):
                decode(frame)

    def test_state_rejects_unvalidated_types(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Backend(Path(directory) / 'settings.json').snapshot()
        self.assertEqual(validate_state(state), state)
        for key, value in [('generation', True), ('preferences_available', 1),
                           ('hardware', 'no'), ('applied', []), ('sensor_source', 'iio'),
                           ('support_tier', 'invented')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_state(dict(state, **{key: value}))


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'settings.json'
        self.states, self.unavailable = [], []
        self.client = ProcessClient(self.path)
        self.addCleanup(self.client.stop)
        self.client.start(self.states.append, lambda: self.unavailable.append(True))

    def until(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.client.poll()
            if predicate():
                return
            time.sleep(.01)
        self.fail('backend did not reach expected state')

    def test_real_child_push_persistence_and_shutdown(self):
        self.until(lambda: bool(self.states))
        future = self.client.set_preference('stand_prompt', True)
        self.until(future.done)
        future.result()
        self.until(lambda: self.states[-1]['stand_prompt'])
        self.assertIsNone(self.states[-1]['applied'])
        process = self.client.process
        self.client.stop()
        self.assertIsNotNone(process.poll())
        self.assertTrue(Backend(self.path).snapshot()['stand_prompt'])

    @unittest.skipUnless(os.name == 'nt', 'Windows venv launchers create a second process')
    def test_venv_backend_is_directly_owned_and_releases_lock_before_stop_returns(self):
        self.until(lambda: bool(self.states))
        base = Path(getattr(sys, '_base_executable', sys.executable)).with_name('python.exe')
        self.assertEqual(Path(self.client.process.args[0]), base)
        self.client.stop()
        with InstanceLock(self.path):
            pass

    def test_disconnect_fails_pending_and_reconnects_with_new_instance(self):
        self.until(lambda: bool(self.states))
        instance = self.states[-1]['instance']
        self.client.process.kill()
        self.client.process.wait(timeout=3)
        future = self.client.set_preference('stand_prompt', True)
        self.until(lambda: future.done() and bool(self.unavailable))
        self.assertIsNotNone(future.exception())
        self.until(lambda: self.states[-1]['instance'] != instance)
        self.assertFalse(self.states[-1]['stand_prompt'])

    def test_unknown_method_cannot_execute_arbitrary_code(self):
        self.until(lambda: bool(self.states))
        future = self.client.request('__import__', {'name': 'os'})
        self.until(future.done)
        self.assertIsNotNone(future.exception())
        self.assertIsNone(self.client.process.poll())
        self.until(lambda: log_path(self.path).is_file() and
                   'backend request rejected' in log_path(self.path).read_text(encoding='utf-8'))

    def test_queue_bound_and_timeout_fail_all_without_replay(self):
        self.until(lambda: bool(self.states))
        # Isolate replies from this test's owner loop; the real pipe reader
        # still has its bounded original inbox and will stop during cleanup.
        self.client.inbox = queue.Queue()
        futures = [self.client.request('get_state', {}) for _ in range(32)]
        rejected = self.client.request('get_state', {})
        self.assertTrue(rejected.done())
        self.assertIsNotNone(rejected.exception())
        now = time.monotonic() + 10
        self.client.clock = lambda: now
        self.client.poll()
        self.assertTrue(all(future.done() and future.exception() for future in futures))
        self.assertIsNone(self.client.process)
        self.assertTrue(self.unavailable)

    def test_invalid_state_is_not_published(self):
        self.until(lambda: bool(self.states))
        before = len(self.states)
        state = dict(self.states[-1], hardware='invalid')
        self.client.inbox.put({'version': 1, 'type': 'state',
                               'instance': state['instance'], 'state': state})
        self.client.poll()
        self.assertEqual(len(self.states), before)
        self.assertIsNone(self.client.process)

    def test_fatal_startup_reason_is_published_without_reconnecting(self):
        self.until(lambda: bool(self.states))
        process = self.client.process
        self.client.inbox.put({
            'version': 1,
            'type': 'fatal',
            'reason': 'slate-configuration-required',
        })
        self.client.poll()
        self.assertEqual(self.client.startup_failure, 'slate-configuration-required')
        self.assertIsNone(self.client.process)
        self.assertIsNotNone(process.poll())
        self.until(lambda: bool(self.unavailable))
        self.client.retry_at = 0
        self.client.poll()
        self.assertIsNone(self.client.process)

    def test_idle_backend_that_stops_responding_is_disconnected_and_reconnected(self):
        self.until(lambda: bool(self.states))
        instance = self.states[-1]['instance']
        # Keep the child and pipes alive but withhold responses from the UI.
        # This models a stalled backend without suspending a host process.
        self.client.inbox = queue.Queue()
        self.client.outbox = queue.Queue()
        now = [self.client.clock()]
        self.client.clock = lambda: now[0]
        now[0] += self.client.timeout
        self.client.poll()
        self.assertTrue(self.client.pending, 'idle connections need a liveness probe')
        request = decode(self.client.outbox.get_nowait())
        self.assertEqual((request['method'], request['params']), ('get_state', {}))
        probe = self.client.pending[request['id']][0]
        self.client.poll()
        self.assertEqual(len(self.client.pending), 1)
        self.assertTrue(self.client.outbox.empty())
        self.assertIsNotNone(self.client.process)
        now[0] += self.client.timeout + .1
        self.client.poll()
        self.assertIsNone(self.client.process)
        self.assertTrue(self.unavailable)
        self.assertIsNotNone(probe.exception())
        now[0] += 1
        self.until(lambda: self.states[-1]['instance'] != instance)
        self.assertFalse(self.states[-1]['stand_prompt'])

    def test_healthy_idle_backend_answers_probe_without_reconnecting(self):
        self.until(lambda: bool(self.states))
        instance = self.states[-1]['instance']
        now = [self.client.clock() + self.client.timeout]
        self.client.clock = lambda: now[0]
        self.client.poll()
        self.assertTrue(self.client.pending)
        self.until(lambda: not self.client.pending)
        self.assertEqual(self.states[-1]['instance'], instance)
        self.assertFalse(self.unavailable)


if __name__ == '__main__':
    unittest.main()
