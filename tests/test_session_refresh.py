"""Ongoing display work uses the shared generation, lease and recovery lane."""
from concurrent.futures import Executor, Future
from dataclasses import replace
from types import SimpleNamespace
import unittest

from panasonic_posture.contracts import ErrorCode
from panasonic_posture.model import HardwareStatus, StateSnapshot, Target
from panasonic_posture.session.failures import DisplayError, FailureKind, ServiceError
from panasonic_posture.session.runtime import SessionRuntime
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.session_display import QualifiedRefresh


class Deferred(Executor):
    def __init__(self): self.calls = []
    def submit(self, function, /, *args, **kwargs):
        future = Future()
        self.calls.append((future, function, args, kwargs))
        return future
    def run(self):
        future, function, args, kwargs = self.calls.pop(0)
        try: future.set_result(function(*args, **kwargs))
        except BaseException as error: future.set_exception(error)


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.state = StateSnapshot(instance='fixture', generation=1, session_connected=True,
            supported=True, hardware=False, hardware_status=HardwareStatus.OK,
            target=Target(mode='tablet', tablet=True, rotation=None, disable_internal_input=False))
        self.fail = False
        self.update_error = None
        self.on_refresh = lambda: None
        def update(generation, ready, code, detail):
            if self.update_error: raise self.update_error
            self.events.append(('feedback', generation, ready))
            if generation == self.state.generation:
                self.state = replace(self.state, error_code=code or None)
        self.client = SimpleNamespace(get_state=lambda: self.state, session_update=update)
        self.display = SimpleNamespace(apply=lambda target: self.events.append(('apply', target.mode)),
            restore=lambda: self.events.append(('restore',)), hold_orientation=lambda: None)
        def refresh(state):
            self.events.append(('refresh', state.generation))
            self.on_refresh()
            if self.fail: raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'orientation-lost')
        self.refresher = SimpleNamespace(needs_refresh=lambda target: target.mode == 'tablet', refresh=refresh)
        self.runtime = SessionRuntime(self.client, self.display, None,
            executor=InlineExecutor(), lease_executor=InlineExecutor(), display_refresh=self.refresher)
        self.addCleanup(self.runtime.close)

    def test_refresh_is_verified_and_rate_limited_on_the_existing_display_lane(self):
        self.runtime.tick(0.)
        self.assertTrue(self.runtime.ready)
        self.assertEqual([e for e in self.events if e[0] == 'refresh'], [('refresh', 1)])
        self.assertIn(('feedback', 1, True), self.events)
        self.runtime.tick(0.)
        self.assertEqual(len([e for e in self.events if e[0] == 'refresh']), 1)
        self.runtime.tick(.2)
        self.assertEqual(len([e for e in self.events if e[0] == 'refresh']), 2)
        self.assertEqual(len([e for e in self.events if e[0] == 'apply']), 1)

    def test_fixed_modes_and_default_linux_runtime_do_not_poll_the_optional_port(self):
        self.state = replace(self.state, target=Target(mode='tent'))
        self.runtime.tick(0.)
        self.runtime.tick(.2)
        self.assertFalse(any(e[0] == 'refresh' for e in self.events))
        self.runtime._display_refresh = None
        self.state = replace(self.state, generation=2, target=Target(mode='tablet'))
        self.runtime.tick(.3)
        self.assertTrue(self.runtime.ready)
        self.assertFalse(any(e[0] == 'refresh' for e in self.events))

    def test_failed_refresh_withdraws_success_before_restore_and_recovers_through_retry(self):
        self.fail = True
        self.runtime.tick(0.)
        self.assertFalse(self.runtime.ready)
        feedback = self.events.index(('feedback', 1, False))
        self.assertGreater(self.events.index(('restore',)), feedback)
        self.assertNotIn(('feedback', 1, True), self.events)
        self.fail = False
        self.runtime.tick(self.runtime.retry_at + .01)
        self.assertTrue(self.runtime.ready)
        self.assertIn(('feedback', 1, True), self.events)

    def test_target_changed_during_refresh_never_acknowledges_the_stale_generation(self):
        self.on_refresh = lambda: setattr(self, 'state', replace(self.state, generation=2, target=Target(mode='stand')))
        self.runtime.tick(0.)
        self.assertNotIn(('feedback', 1, True), self.events)
        self.assertIn(('apply', 'stand'), self.events)
        self.assertIn(('feedback', 2, True), self.events)

    def test_retry_clears_echoed_error_before_the_next_qualified_refresh(self):
        self.runtime._display_refresh = QualifiedRefresh(self.refresher,
            SimpleNamespace(control_available=lambda: True), lambda: self.state)
        self.fail = True
        self.runtime.tick(0.)
        self.assertEqual(self.state.error_code, ErrorCode.DISPLAY_UNAVAILABLE)
        self.fail = False
        now = self.runtime.retry_at + .01
        self.runtime.tick(now)
        self.assertTrue(self.runtime.ready)
        self.assertIsNone(self.state.error_code)
        self.runtime.tick(now + .2)
        self.assertTrue(self.runtime.ready)
        self.assertIsNone(self.state.error_code)

    def test_pending_refresh_keeps_lease_on_independent_lane_without_parallel_display_writes(self):
        self.runtime.tick(0.)
        executor = Deferred()
        self.runtime._executor = executor
        self.runtime.tick(.2)
        executor.run()  # state -> queued refresh
        self.runtime.drain_completed_tasks(.2)
        self.assertEqual(self.runtime._tasks[0].kind, 'refresh')
        before = len(self.events)
        self.runtime.tick(1.3)
        self.assertEqual(self.events[before:], [('feedback', 1, False)])
        executor.run()  # refresh -> verify
        self.runtime.drain_completed_tasks(1.3)
        self.assertEqual(self.runtime._tasks[0].kind, 'verify')
        # A deferred test executor must be emptied before runtime shutdown.
        self.runtime._executor = InlineExecutor()
        executor.run()
        self.runtime.drain_completed_tasks(1.3)
        self.assertTrue(self.runtime.ready)

    def test_rejection_during_refresh_revokes_readiness_and_restores_after_the_inflight_write(self):
        self.runtime.tick(0.)
        executor = Deferred()
        self.runtime._executor = executor
        self.runtime.tick(.2)
        executor.run()
        self.runtime.drain_completed_tasks(.2)
        self.update_error = ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, 'lost', kind=FailureKind.REJECTED)
        self.runtime.tick(1.3)
        self.assertTrue(self.runtime.failure_active)
        self.assertFalse(self.runtime.ready)
        self.assertEqual([task.kind for task in self.runtime._tasks], ['refresh', 'restore'])
        executor.run()
        self.runtime.drain_completed_tasks(1.3)
        executor.run()
        self.runtime.drain_completed_tasks(1.3)
        self.runtime._executor = InlineExecutor()
        self.assertEqual(self.events[-1], ('restore',))


class QualificationTests(unittest.TestCase):
    def test_current_generation_and_session_are_checked_on_both_sides_of_refresh(self):
        state = StateSnapshot(instance='fixture', generation=1, supported=True, session_connected=True,
            hardware=False, hardware_status=HardwareStatus.OK, target=Target(mode='tablet'))
        current = [state]
        context = SimpleNamespace(control_available=lambda: True)
        calls = []
        port = SimpleNamespace(needs_refresh=lambda target: target.mode == 'tablet',
            refresh=lambda snapshot: calls.append(snapshot))
        guarded = QualifiedRefresh(port, context, lambda: current[0])
        for changes in (dict(generation=2), dict(instance='new'), dict(session_connected=False),
                        dict(hardware=True), dict(hardware_status=HardwareStatus.AMBIGUOUS),
                        dict(sensor_error='lost'), dict(error_code=ErrorCode.INTERNAL),
                        dict(target=Target(mode='tent')), dict(supported=False)):
            current[0] = replace(state, **changes)
            with self.subTest(changes=changes), self.assertRaises(DisplayError): guarded.refresh(state)
        self.assertEqual(calls, [])
        current[0] = state
        guarded.refresh(state)
        self.assertEqual(calls, [state])
        port.refresh = lambda snapshot: current.__setitem__(0, replace(state, generation=2))
        with self.assertRaises(DisplayError): guarded.refresh(state)
        current[0] = state
        context.control_available = lambda: False
        with self.assertRaises(DisplayError): guarded.refresh(state)


if __name__ == '__main__': unittest.main()
