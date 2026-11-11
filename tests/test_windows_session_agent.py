"""Native session assembly must prove receipt authority before desktop work."""
from dataclasses import replace
from concurrent.futures import Future
import ctypes as C
from types import SimpleNamespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from panasonic_posture.model import HardwareStatus, StateSnapshot, Target
from panasonic_posture.session.failures import ServiceError, FailureKind
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.session_agent import (
    WindowsSessionAgent,
    build_display,
    build_optional_slate,
    main,
)
from panasonic_posture.windows.security import WindowsSecurity, AuthenticationError


class Context:
    def __init__(self):
        self.absent = False
        self.screen_off = False
        self.closed = False
    def unattended(self):
        return self.absent or self.screen_off
    def control_available(self):
        return not self.absent
    def poll(self):
        pass
    def close(self):
        self.closed = True


class Client:
    def __init__(self):
        self.state = StateSnapshot(instance='native-fixture', hardware=False, sensor_source='windows-winrt',
            target=Target(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
        self.feedback = []
        self.release_pose = None
        self.closed = False
        self.deny = False
    def get_state(self):
        return self.state
    def session_update(self, generation, ready, error_code=None, error_detail=None):
        if self.deny:
            raise ServiceError('access-denied', 'unregistered-session-process')
        self.feedback.append((generation, ready, error_code, error_detail))
        self.state = replace(self.state, session_connected=True)
    def close(self):
        self.closed = True
    def hardware_release(self):
        return self.state, self.release_pose


class Display:
    def __init__(self):
        self.current = 0
        self.writes = []
        self.during_apply = lambda: None
    def apply(self, target):
        self.current = 2
        self.writes.append(2)
        self.during_apply()
    def restore(self, normal=False):
        self.current = 0
        self.writes.append(0)
    def hold_orientation(self):
        self.writes.append('hold')
    def prepare_hardware_release(self):
        self.writes.append('prepare')


class SessionAgentTests(unittest.TestCase):
    def setUp(self):
        self.context, self.client, self.display = Context(), Client(), Display()
        self.created = []
        def display(client):
            self.assertIsNotNone(client)
            self.created.append(True)
            self.assertTrue(self.client.feedback)
            self.assertFalse(self.client.feedback[0][1])
            return self.display
        self.agent = WindowsSessionAgent(context_factory=lambda: self.context,
            client_factory=lambda unattended: self.client, display_factory=display,
            executor=InlineExecutor(), lease_executor=InlineExecutor())
        self.addCleanup(self.agent.close)

    def test_denied_startup_cannot_construct_a_display_or_write_a_journal(self):
        self.client.deny = True
        with self.assertRaises(ServiceError):
            self.agent.start()
        self.assertFalse(self.created)
        self.assertFalse(self.display.writes)
        self.assertTrue(self.client.closed)
        self.assertTrue(self.context.closed)

    def test_authorized_agent_runs_shared_transactions_then_restores_on_close(self):
        self.agent.start()
        self.agent.tick(10.)
        self.assertTrue(self.agent.runtime.ready)
        self.assertEqual(self.display.current, 2)
        self.assertTrue(any(ready for _, ready, _, _ in self.client.feedback))
        self.agent.close()
        self.assertEqual(self.display.current, 0)
        self.assertTrue(self.client.closed)
        self.assertTrue(self.context.closed)

    def test_physical_release_uses_cached_rpc_metadata_before_runtime_tick(self):
        self.agent.start()
        self.client.state = replace(self.client.state, hardware=True)
        self.client.release_pose = 'tent'
        self.agent.tick(10.)
        self.assertEqual(self.display.writes[0], 'prepare')

    def test_screen_off_is_context_and_does_not_reject_valid_display_control(self):
        self.context.screen_off = True
        self.assertTrue(self.context.unattended())
        self.agent.start()
        self.agent.tick(10.)
        self.assertTrue(self.agent.runtime.ready)
        self.assertEqual(self.display.current, 2)
        self.assertTrue(self.client.feedback[-1][1])

    def test_qualification_loss_reports_not_ready_and_restores_owned_direction(self):
        self.agent.start()
        self.agent.tick(10.)
        self.context.absent = True
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.agent.tick(10.6)
        self.assertTrue(self.agent.runtime.failure_active)
        self.assertEqual(self.display.current, 0)
        self.assertFalse(self.client.feedback[-1][1])
        self.context.absent = False
        self.agent.tick(13.)
        self.assertFalse(self.agent.runtime.failure_active)
        self.assertTrue(self.agent.runtime.ready)
        self.assertEqual(self.display.current, 2)

    def test_unknown_context_at_startup_cannot_apply_a_new_rotation(self):
        self.context.absent = True
        self.agent.start()
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.agent.tick(10.)
        self.assertNotIn(2, self.display.writes)
        self.assertTrue(all(not ready for _, ready, _, _ in self.client.feedback))

    def test_context_loss_during_apply_rolls_back_before_ready_feedback(self):
        self.agent.start()
        self.display.during_apply = lambda: setattr(self.context, 'absent', True)
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.agent.tick(10.)
        self.assertEqual(self.display.current, 0)
        self.assertFalse(self.agent.runtime.ready)
        self.assertTrue(all(not ready for _, ready, _, _ in self.client.feedback))

    def test_unproven_or_replaced_service_does_not_construct_a_display(self):
        self.client.session_update = lambda *args: setattr(self.client, 'state',
            replace(self.client.state, instance='replacement', session_connected=True))
        with self.assertRaises(ServiceError) as caught:
            self.agent.start()
        self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        self.assertFalse(self.created)

    def test_late_unavailable_heartbeat_finishes_before_resumed_ready_feedback(self):
        jobs = []
        class Worker:
            def submit(self, function):
                future = Future()
                jobs.append((function, future))
                return future
        self.agent.lease_executor = Worker()
        self.agent.start()
        self.agent.tick(10.)
        self.context.absent = True
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.agent.tick(10.6)
        self.assertEqual(len(jobs), 1)
        for _ in range(10):
            self.agent.tick(11.)
        self.assertEqual(len(jobs), 1)
        self.context.absent = False
        self.agent.tick(13.)
        self.assertTrue(self.agent.runtime.failure_active)
        self.assertEqual(self.display.current, 0)
        function, future = jobs[0]
        function()
        future.set_result(None)
        last_false = len(self.client.feedback) - 1
        self.agent.tick(13.1)
        self.assertTrue(self.agent.runtime.ready)
        self.assertEqual(self.display.current, 2)
        self.assertEqual([ready for _, ready, _, _ in self.client.feedback[last_false + 1:]], [False, True])

    def test_restore_followed_by_quick_resume_still_requires_a_new_transaction(self):
        self.agent.start()
        self.display.during_apply = lambda: setattr(self.context, 'absent', True)
        restore = self.display.restore
        def resumed_restore(normal=False):
            restore(normal)
            self.context.absent = False
        self.display.restore = resumed_restore
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.agent.tick(10.)
        self.assertEqual(self.display.current, 0)
        self.assertFalse(self.agent.runtime.ready)
        self.assertTrue(all(not ready for _, ready, _, _ in self.client.feedback))

    def test_slate_receives_the_qualified_client_and_closes_after_display_release(self):
        events = []
        class Slate:
            def sync_mode(self, mode):
                events.append(('sync', mode))
                return True
            def release(self):
                events.append(('release', None))
            def close(self):
                events.append(('close', None))
        display = self.display
        restore = display.restore
        def ordered_restore(normal=False):
            restore(normal)
            events.append(('display-restore', normal))
        display.restore = ordered_restore
        agent = WindowsSessionAgent(context_factory=lambda: self.context,
            client_factory=lambda unattended: self.client,
            display_factory=lambda client: display,
            slate_factory=lambda client: Slate(),
            executor=InlineExecutor(), lease_executor=InlineExecutor())
        agent.start()
        agent.tick(10.)
        self.assertIn(('sync', 'tent'), events)
        agent.close()
        self.assertEqual(events[-3:], [
            ('display-restore', False), ('release', None), ('close', None),
        ])

    def test_missing_csm_degrades_to_display_control_without_constructing_a_notifier(self):
        created = []

        def slate(client):
            self.assertIsNotNone(client)
            created.append(True)
            return None

        agent = WindowsSessionAgent(context_factory=lambda: self.context,
            client_factory=lambda unattended: self.client,
            display_factory=lambda client: self.display,
            slate_factory=slate,
            executor=InlineExecutor(), lease_executor=InlineExecutor())
        self.addCleanup(agent.close)
        agent.start()
        agent.tick(10.)
        self.assertEqual(created, [True])
        self.assertEqual(self.display.current, 2)
        self.assertTrue(agent.runtime.ready)

    def test_unavailable_csm_returns_none_before_any_indicator_object_exists(self):
        class Denied:
            def __init__(self):
                raise OSError('LaptopSlateExclusiveAccess=1')

        with tempfile.TemporaryDirectory() as directory, \
                patch('panasonic_posture.windows.convertible_slate.NativeSlateDevice', Denied), \
                self.assertLogs('panasonic_posture.windows.session_agent', level='WARNING') as logged:
            result = build_optional_slate(self.client, Path(directory) / 'display.json')
        self.assertIsNone(result)
        self.assertIn('continuing without CSM', '\n'.join(logged.output))

    def test_display_recovery_is_gated_by_the_authenticated_physical_query(self):
        state = StateSnapshot(hardware=None, hardware_status=HardwareStatus.ABSENT)
        client = SimpleNamespace(get_state=lambda: state)
        with tempfile.TemporaryDirectory() as directory, patch(
            'panasonic_posture.windows.display.WindowsDisplay'
        ) as display:
            build_display(client, Path(directory) / 'display.json')
        query = display.call_args.kwargs['hardware_query']
        self.assertEqual(query(), (None, HardwareStatus.ABSENT))

    def test_available_csm_receives_the_shared_hardware_query(self):
        created = {}

        class Device:
            pass

        class Notifier:
            def __init__(self, device, journal, *, hardware_query):
                created.update(device=device, journal=journal, query=hardware_query)

        class Client:
            @staticmethod
            def get_state():
                return 'snapshot'

        with tempfile.TemporaryDirectory() as directory, \
                patch('panasonic_posture.windows.convertible_slate.NativeSlateDevice', Device), \
                patch('panasonic_posture.windows.convertible_slate.SlateNotifier', Notifier):
            result = build_optional_slate(Client(), Path(directory) / 'display.json')
        self.assertIsInstance(created['device'], Device)
        self.assertIsInstance(result, Notifier)
        from panasonic_posture.windows.session_display import SessionHardwareQuery
        self.assertIsInstance(created['query'], SessionHardwareQuery)


class NativeAgentTokenTests(unittest.TestCase):
    def test_launcher_stop_event_exits_through_runtime_cleanup_before_closing_event(self):
        events = []
        signal = SimpleNamespace(signaled=False)
        signal.is_set = lambda: signal.signaled
        signal.close = lambda: events.append('event-close')
        def tick(now):
            events.append('tick')
            signal.signaled = True
        agent = SimpleNamespace(start=lambda: events.append('start'), tick=tick,
                                close=lambda: events.append('restore'))
        with tempfile.TemporaryDirectory() as directory:
            with patch('panasonic_posture.windows.session_agent.WindowsSessionAgent', return_value=agent), \
                 patch('panasonic_posture.windows.session_stop.NativeStopSignal.open', return_value=signal), \
                 patch('panasonic_posture.windows.diagnostics.configure_logging'), \
                 patch('panasonic_posture.windows.session_agent.sys.platform', 'win32'), \
                 patch('panasonic_posture.windows.session_agent.time.sleep'):
                result = main(['--journal', str(Path(directory) / 'display.json'), '--stop-event', 'fixture'])
        self.assertEqual(result, 0)
        self.assertEqual(events, ['start', 'tick', 'restore', 'event-close'])

    def security(self, *, sid='S-1-5-21-1000', session=1, elevation=0, length=4):
        api = object.__new__(WindowsSecurity)
        closed = []
        def open_token(process, access, token):
            token._obj.value = 12
            return True
        api.kernel = SimpleNamespace(GetCurrentProcess=lambda: 10, CloseHandle=lambda value: closed.append(value.value))
        api.advapi = SimpleNamespace(OpenProcessToken=open_token)
        api.token_identity = lambda token: (sid, 100, session)
        api._token_info = lambda token, kind: C.create_string_buffer(elevation.to_bytes(4, 'little')[:length], length)
        return api, closed

    def test_agent_requires_an_ordinary_non_elevated_interactive_token(self):
        api, closed = self.security()
        api.require_unprivileged()
        self.assertEqual(closed, [12])
        for changes in ({'sid': 'S-1-5-18'}, {'session': 0}, {'elevation': 1}, {'length': 2}):
            api, closed = self.security(**changes)
            with self.assertRaises(AuthenticationError):
                api.require_unprivileged()
            self.assertEqual(closed, [12])
