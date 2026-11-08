"""Hardware UI lifecycle: owner-loop callbacks and bounded, non-replayed RPC."""
from dataclasses import replace
import threading
import time
import unittest

from panasonic_posture.model import StateSnapshot
from panasonic_posture.windows.hardware_client import HardwareUIClient
from panasonic_posture.windows.hardware_ipc import HardwareOperationError, validated_hardware_state


class Connection:
    def __init__(self, instance='one'):
        self.state = StateSnapshot(instance=instance, hardware=False, sensor_source='windows-winrt')
        self.calls = []
        self.closed = threading.Event()
        self.entered = threading.Event()
        self.gate = None
        self.error = None

    def request(self, method, params):
        self.calls.append((method, params))
        if self.gate is not None and method != 'get_state':
            self.entered.set()
            if not self.gate.wait(4):
                raise TimeoutError('fixture operation did not release')
        if self.error is not None:
            raise self.error
        if method == 'set_preference':
            self.state = replace(self.state, **{params['field']: params['enabled']})
        return dict(instance=self.state.instance, accepted=True, state=self.state.to_dict())

    def close(self):
        self.closed.set()


class HardwareUIClientTests(unittest.TestCase):
    def setUp(self):
        self.states, self.unavailable, self.events = [], [], []
        self.owner = threading.get_ident()
        self.connection = Connection()
        self.client = HardwareUIClient(factory=lambda: self.connection)

    def start(self):
        self.client.start(lambda value: (self.states.append(value), self.events.append(('state', threading.get_ident()))),
                          lambda: self.unavailable.append(True),
                          lambda: self.events.append(('connected', threading.get_ident())))
        self.addCleanup(self.shutdown)
        self.until(lambda: bool(self.states))

    def shutdown(self):
        self.client.stop()
        if self.connection.gate is not None:
            self.connection.gate.set()
        self.client._thread.join(timeout=5)
        self.assertFalse(self.client._thread.is_alive())

    def until(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.client.poll()
            if predicate():
                return
            time.sleep(.01)
        self.fail('hardware UI client did not reach expected state')

    def test_state_precedes_completion_and_every_callback_uses_owner_loop(self):
        self.start()
        future = self.client.set_preference('stand_prompt', True)
        seen = []
        future.add_done_callback(lambda result: seen.append((self.states[-1]['stand_prompt'], threading.get_ident())))
        self.until(future.done)
        future.result()
        self.assertEqual(seen, [(True, self.owner)])
        self.assertTrue(all(owner == self.owner for _, owner in self.events))
        self.assertFalse(self.states[-1]['simulation'])

    def test_retry_does_not_inject_a_mode_write_and_ui_cannot_submit_receipts(self):
        self.start()
        future = self.client.retry('tent')
        self.until(future.done)
        self.assertEqual(self.connection.calls[-1], ('retry', {}))
        rejected = self.client._request('session_update', {})
        self.assertIsNotNone(rejected.exception())
        self.assertFalse(any(method in ('set_mode', 'session_update') for method, _ in self.connection.calls))

    def test_rejected_operation_publishes_valid_state_and_preserves_connection(self):
        self.start()
        self.connection.error = HardwareOperationError('config-write-failed', validated_hardware_state(self.connection.state.to_dict()))
        future = self.client.set_preference('stand_prompt', True)
        self.until(future.done)
        self.assertEqual(future.exception().code, 'config-write-failed')
        self.assertEqual(self.client.instance, 'one')
        self.assertFalse(self.connection.closed.is_set())
        self.assertFalse(self.unavailable)

    def test_stop_invalidates_callbacks_before_failing_futures_without_blocking_on_io(self):
        self.start()
        self.connection.gate = threading.Event()
        future = self.client.set_mode('tent')
        self.assertTrue(self.connection.entered.wait(3))
        failed = []
        future.add_done_callback(lambda result: failed.append(bool(self.unavailable)))
        self.client.stop()
        self.assertEqual(failed, [True])
        self.assertFalse(self.connection.closed.is_set())
        self.connection.gate.set()
        self.assertTrue(self.connection.closed.wait(3))

    def test_stale_queued_state_never_revives_a_connection_or_completes_an_operation(self):
        now = [100.]
        self.client.clock = lambda: now[0]
        self.start()
        future = self.client.set_preference('stand_prompt', True)
        deadline = time.monotonic() + 3
        while self.client._inbox.empty() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.client._inbox.empty())
        now[0] += 4
        self.client.poll()
        self.assertIsNotNone(future.exception())
        self.assertFalse(self.states[-1]['stand_prompt'])
        self.assertIsNone(self.client.instance)

    def test_slow_state_callback_cannot_complete_an_expired_operation_successfully(self):
        now = [100.]
        self.client.clock = lambda: now[0]
        self.start()
        publish = self.client._state
        def slow_state(value):
            publish(value)
            now[0] += 6
        self.client._state = slow_state
        future = self.client.set_preference('stand_prompt', True)
        self.until(future.done)
        self.assertIsNotNone(future.exception())
        self.assertTrue(self.unavailable)
        self.assertIsNone(self.client.instance)

    def test_queue_and_reconnect_are_bounded_and_no_write_is_replayed(self):
        now = [100.]
        made = []
        def factory():
            connection = self.connection if not made else Connection('two')
            made.append(connection)
            return connection
        self.client = HardwareUIClient(factory=factory, clock=lambda: now[0])
        self.start()
        self.connection.gate = threading.Event()
        futures = [self.client.set_preference('stand_prompt', True) for _ in range(32)]
        self.assertTrue(self.connection.entered.wait(3))
        self.assertIsNotNone(self.client.set_mode('tent').exception())
        now[0] += 6
        self.client.poll()
        self.assertTrue(all(future.done() and future.exception() for future in futures))
        now[0] += 1
        self.client.poll()
        self.assertEqual(len(made), 1)
        self.connection.gate.set()
        self.assertTrue(self.connection.closed.wait(3))
        self.until(lambda: self.client.instance == 'two')
        self.assertFalse(any(method == 'set_preference' for method, _ in made[1].calls))
        self.assertEqual(sum(method == 'set_preference' for method, _ in made[0].calls), 1)

    def test_invalid_capabilities_are_not_published(self):
        self.start()
        count = len(self.states)
        self.connection.state = replace(self.connection.state, sensor_source='iio')
        self.client.refresh()
        self.until(lambda: bool(self.unavailable))
        self.assertEqual(len(self.states), count)
        self.assertIsNone(self.client.instance)

    def test_service_instance_change_invalidates_old_results(self):
        self.start()
        self.connection.state = replace(self.connection.state, instance='replacement')
        future = self.client.set_mode('tent')
        self.until(future.done)
        self.assertIsNotNone(future.exception())
        self.assertIsNone(self.client.instance)

    def test_public_operations_reject_calls_from_another_thread(self):
        self.start()
        errors = []
        def wrong_thread():
            try:
                self.client.set_mode('tent')
            except RuntimeError as error:
                errors.append(error)
        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join(timeout=3)
        self.assertEqual(len(errors), 1)
