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
        self.refresh_fails = False
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
            if self.refresh_fails: raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'orientation-lost')
        self.refresher = SimpleNamespace(needs_refresh=lambda target: target.mode == 'tablet', refresh=refresh)
        self.runtime = SessionRuntime(self.client, self.display, None,
            executor=InlineExecutor(), lease_executor=InlineExecutor(), display_refresh=self.refresher)
        self.addCleanup(self.runtime.close)

    def test_refresh_is_verified_and_rate_limited_on_the_existing_display_lane(self):
        self.runtime.tick(0.)
        self.assertTrue(self.runtime.ready)
        self.runtime.tick(0.)
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
        self.refresh_fails = True
        self.runtime.tick(0.)
        self.runtime.tick(0.)
        self.assertFalse(self.runtime.ready)
        success = self.events.index(('feedback', 1, True))
        feedback = self.events.index(('feedback', 1, False))
        self.assertGreater(feedback, success)
        self.assertGreater(self.events.index(('restore',)), feedback)
        self.refresh_fails = False
        self.runtime.tick(self.runtime.retry_at + .01)
        self.assertTrue(self.runtime.ready)
        self.assertIn(('feedback', 1, True), self.events)

    def test_target_changed_during_refresh_never_acknowledges_the_stale_generation(self):
        self.on_refresh = lambda: setattr(self, 'state', replace(self.state, generation=2, target=Target(mode='stand')))
        self.runtime.tick(0.)
        self.runtime.tick(0.)
        refresh = self.events.index(('refresh', 1))
        self.assertNotIn(('feedback', 1, True), self.events[refresh + 1:])
        self.assertIn(('apply', 'stand'), self.events)
        self.assertIn(('feedback', 2, True), self.events)

    def test_retry_clears_echoed_error_before_the_next_qualified_refresh(self):
        self.runtime._display_refresh = QualifiedRefresh(self.refresher,
            SimpleNamespace(control_available=lambda: True), lambda: self.state)
        self.refresh_fails = True
        self.runtime.tick(0.)
        self.runtime.tick(0.)
        self.assertEqual(self.state.error_code, ErrorCode.DISPLAY_UNAVAILABLE)
        self.refresh_fails = False
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

    def test_completed_apply_reports_readiness_before_a_new_refresh_can_replace_it(self):
        executor = Deferred()
        self.runtime._executor = executor
        self.runtime.tick(0.)
        # state -> apply -> verify
        for now in (0., 0., 0.):
            executor.run()
            self.runtime.drain_completed_tasks(now)
        executor.run()  # ready feedback precedes refresh
        self.runtime.drain_completed_tasks(0.)

        self.runtime.tick(.2)  # state -> queued refresh
        executor.run()
        self.runtime.drain_completed_tasks(.2)
        self.assertEqual(self.runtime._tasks[0].kind, 'refresh')
        executor.run()
        self.runtime.drain_completed_tasks(.2)
        executor.run()  # verify without another due refresh at the same time
        self.runtime.drain_completed_tasks(.2)
        self.assertEqual([event for event in self.events if event[0] == 'feedback'],
                         [('feedback', 1, True)])
        self.runtime._executor = InlineExecutor()

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


class HardwareHandoffTests(unittest.TestCase):
    def make_runtime(self, state, *, defer=True):
        events = []
        current = [state]

        def update(generation, ready, code, detail):
            del code, detail
            events.append(('feedback', generation, ready))

        client = SimpleNamespace(get_state=lambda: current[0], session_update=update)
        display = SimpleNamespace(
            apply=lambda target: events.append(('apply', target.mode)),
            hold_orientation=lambda: events.append(('hold',)),
            restore=lambda: events.append(('restore',)),
        )
        runtime = SessionRuntime(
            client,
            display,
            None,
            executor=InlineExecutor(),
            lease_executor=InlineExecutor(),
            defer_hardware_handoff=defer,
        )
        self.addCleanup(runtime.close)
        return events, runtime, current

    def test_windows_release_holds_across_the_settled_target_generation(self):
        state = StateSnapshot(
            instance='fixture',
            generation=1,
            session_connected=True,
            supported=True,
            hardware=False,
            hardware_status=HardwareStatus.OK,
            posture_pending=True,
            target=Target(mode='unavailable'),
        )
        events, runtime, current = self.make_runtime(state)

        runtime.tick(0.)
        self.assertIn(('hold',), events)
        self.assertFalse(any(event[0] == 'apply' for event in events))

        current[0] = replace(
            state,
            generation=2,
            posture_pending=False,
            target=Target(mode='tent'),
        )
        runtime.tick(.2)
        runtime.tick(1.9)
        self.assertNotIn(('apply', 'tent'), events)
        self.assertFalse(runtime.ready)

        runtime.tick(2.01)
        self.assertEqual([event for event in events if event[0] == 'apply'], [('apply', 'tent')])
        self.assertTrue(runtime.ready)
        self.assertEqual(events[-1], ('feedback', 2, True))

    def test_windows_release_hold_also_covers_a_direct_settled_target(self):
        state = StateSnapshot(
            instance='fixture',
            generation=1,
            session_connected=True,
            supported=True,
            hardware=True,
            hardware_status=HardwareStatus.OK,
            target=Target(mode='hardware'),
        )
        events, runtime, current = self.make_runtime(state)

        runtime.tick(0.)
        self.assertEqual([event for event in events if event[0] == 'apply'],
                         [('apply', 'hardware')])

        current[0] = replace(
            state,
            generation=2,
            hardware=False,
            target=Target(mode='tent'),
        )
        runtime.tick(.2)
        runtime.tick(1.9)
        self.assertNotIn(('apply', 'tent'), events)

        runtime.tick(2.21)
        self.assertEqual([event for event in events if event[0] == 'apply'],
                         [('apply', 'hardware'), ('apply', 'tent')])
        self.assertTrue(runtime.ready)

    def test_physical_reentry_cancels_the_windows_release_hold(self):
        state = StateSnapshot(
            instance='fixture',
            generation=1,
            session_connected=True,
            supported=True,
            hardware=False,
            hardware_status=HardwareStatus.OK,
            posture_pending=True,
            target=Target(mode='unavailable'),
        )
        events, runtime, current = self.make_runtime(state)
        runtime.tick(0.)
        self.assertIn(('hold',), events)

        current[0] = replace(
            state,
            generation=2,
            hardware=True,
            posture_pending=False,
            target=Target(mode='hardware'),
        )
        runtime.tick(.1)
        self.assertIn(('apply', 'hardware'), events)
        self.assertTrue(runtime.ready)

    def test_windows_handoff_wait_is_opt_in(self):
        state = StateSnapshot(
            instance='fixture',
            generation=1,
            session_connected=True,
            supported=True,
            hardware=False,
            hardware_status=HardwareStatus.OK,
            posture_pending=True,
            target=Target(mode='unavailable'),
        )
        events, runtime, current = self.make_runtime(state, defer=False)

        current[0] = replace(
            state,
            generation=2,
            posture_pending=False,
            target=Target(mode='tent'),
        )
        runtime.tick(0.)
        self.assertIn(('apply', 'tent'), events)


if __name__ == '__main__': unittest.main()
