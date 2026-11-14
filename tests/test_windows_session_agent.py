"""Native session assembly must prove receipt authority before desktop work."""
from dataclasses import replace
from concurrent.futures import Future, ThreadPoolExecutor
import ctypes as C
from types import SimpleNamespace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from panasonic_posture.model import HardwareStatus, StateSnapshot, Target
from panasonic_posture.session.failures import ServiceError, FailureKind
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.session.agent import (
    WindowsSessionAgent,
    build_display,
    build_optional_slate,
    main,
)
from panasonic_posture.windows.common.security import WindowsSecurity, AuthenticationError


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


class SessionAgentTests(unittest.TestCase):
    def setUp(self):
        self.context, self.client, self.display = Context(), Client(), Display()
        self.created = []
        self.context_callbacks = []
        def display(client):
            self.assertIsNotNone(client)
            self.created.append(True)
            self.assertTrue(self.client.feedback)
            self.assertFalse(self.client.feedback[0][1])
            return self.display
        def client_factory(available, unattended):
            self.context_callbacks.append((available, unattended))
            return self.client
        self.agent = WindowsSessionAgent(context_factory=lambda: self.context,
            client_factory=client_factory, display_factory=display,
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

    def test_agent_wires_both_control_and_unattended_context(self):
        self.agent.start()
        available, unattended = self.context_callbacks[-1]
        self.assertTrue(available())
        self.assertFalse(unattended())
        self.context.screen_off = True
        self.assertTrue(available())
        self.assertTrue(unattended())
        self.context.absent = True
        self.assertFalse(available())
        self.assertTrue(unattended())

    def test_receipt_proof_and_display_recovery_share_the_display_worker(self):
        workers = []

        class RecordingClient(Client):
            def get_state(self):
                workers.append(threading.get_ident())
                return super().get_state()

        self.client = RecordingClient()
        main_thread = threading.get_ident()
        restore_workers = []
        slate_workers = []
        restore = self.display.restore

        class Slate:
            def sync_mode(self, mode):
                return True

            def release(self):
                slate_workers.append(threading.get_ident())

            def close(self):
                slate_workers.append(threading.get_ident())

        def recording_restore(normal=False):
            restore_workers.append(threading.get_ident())
            restore(normal)

        self.display.restore = recording_restore

        def display(client):
            workers.append(threading.get_ident())
            client.get_state()  # Display recovery performs the first RPC.
            return self.display

        executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(executor.shutdown, wait=True, cancel_futures=True)
        agent = WindowsSessionAgent(
            context_factory=lambda: self.context,
            client_factory=lambda available, unattended: self.client,
            display_factory=display,
            slate_factory=lambda client: Slate(),
            executor=executor,
            lease_executor=InlineExecutor(),
        )
        self.addCleanup(agent.close)
        agent.start()
        agent.tick(10.)
        agent.close()

        self.assertNotIn(main_thread, workers)
        self.assertEqual(len(set(workers)), 1)
        self.assertEqual(set(restore_workers), set(workers))
        self.assertEqual(set(slate_workers), set(workers))

    def test_installed_agent_repairs_a_late_unlock_without_rotating_twice(self):
        from tests.test_windows_display import Api
        from tests.windows_fixture import MemoryJournal
        from panasonic_posture.windows.session.display_backend import WindowsDisplay
        self.client.state = replace(self.client.state, hardware_status=HardwareStatus.OK)
        api = Api()
        api.current = replace(api.current, auto_rotation=0)
        self.agent.display_factory = lambda client: WindowsDisplay(
            api=api, journal=MemoryJournal(), hardware_query=lambda: (False, HardwareStatus.OK),
        )
        self.agent.start()
        self.agent.tick(10.)
        self.assertEqual(api.writes, [2])
        api.current = replace(api.current, auto_rotation=0)
        self.agent.tick(10.1)
        self.assertTrue(self.agent.runtime.ready)
        self.assertEqual(api.writes, [2])
        self.assertEqual(api.lock_writes, [True, True])

    def test_presence_aging_between_state_read_and_refresh_keeps_the_owned_display(self):
        self.client.state = replace(self.client.state, hardware_status=HardwareStatus.OK)
        self.display.needs_refresh = lambda target: target.mode == 'tent'
        self.display.refresh = lambda state: self.display.writes.append('refresh')
        self.agent.start()
        self.agent.tick(10.)
        self.context.snapshot = lambda: dict(
            suspended=False, error=None, display_on=True, control_available=False,
            presence_stale=True,
            presence=dict(session_id=1, console_session_id=1, active=True, locked=False),
        )
        checks = 0
        def available():
            nonlocal checks
            checks += 1
            return checks <= 2  # Agent tick and state read succeed; refresh sees stale presence.
        self.context.control_available = available
        before = list(self.client.feedback)
        self.agent.tick(10.1)
        self.assertTrue(self.agent.runtime.ready)
        self.assertEqual(self.display.writes, [2])
        self.assertEqual(self.client.feedback, before)
        self.assertFalse(self.agent.runtime.failure_active)

    def test_installed_display_factory_handles_external_only_without_ignoring_recovery(self):
        from panasonic_posture.windows.session.display_backend import Panel
        from panasonic_posture.session.journal_store import Journal
        from panasonic_posture.session.failures import DisplayError
        api = SimpleNamespace(panels=lambda: [Panel('external', r'\\.\DISPLAY2',
                                                  (1, 0, 1), 5, 0, 1920, 1080, 0)])
        with tempfile.TemporaryDirectory() as directory, patch(
            'panasonic_posture.windows.session.display_backend.NativeDisplay', return_value=api,
        ):
            path = Path(directory) / 'display.json'
            selected = build_display(self.client, path)
            self.assertEqual(selected.backend_name, 'windows-external-only')
            Journal(path).save(dict(version=1, backend='windows', spec=['internal'],
                                    original_transform=0, original_lock=False))
            with self.assertRaises(DisplayError) as caught:
                build_display(self.client, path)
            self.assertEqual(caught.exception.detail, 'external-only-display-recovery-pending')

    def test_unavailable_target_renews_the_lease_without_reporting_ready(self):
        self.client.state = replace(
            self.client.state,
            hardware=None,
            target=Target(mode='unavailable'),
            session_connected=False,
        )
        self.agent.start()
        feedback_before = len(self.client.feedback)
        for now in (10., 10.2, 10.6, 11.1):
            self.agent.tick(now)
        self.assertGreater(len(self.client.feedback), feedback_before)
        self.assertTrue(all(not ready for _, ready, _, _ in self.client.feedback))
        self.assertFalse(self.agent.runtime.ready)

    def test_physical_release_never_prepares_the_display_before_the_switch_opens(self):
        self.agent.start()
        self.client.state = replace(
            self.client.state,
            hardware=True,
            target=Target(mode='hardware'),
        )
        self.client.release_pose = 'tent'
        self.agent.tick(10.)
        self.assertNotIn(('prepare', 'tent'), self.display.writes)
        self.assertEqual(self.display.writes, [2])

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

    def test_stale_presence_keeps_ready_and_does_not_report_lost_within_grace(self):
        self.context.snapshot = lambda: dict(
            suspended=False, error=None, display_on=True, control_available=False,
            presence_stale=True,
            presence=dict(session_id=1, console_session_id=1, active=True, locked=False),
        )
        self.agent.start()
        self.agent.tick(10.)
        feedback_before = len(self.client.feedback)
        writes_before = list(self.display.writes)
        self.context.absent = True
        for now in (10.6, 11., 12., 13.5):
            self.agent.tick(now)
        self.assertEqual(self.client.feedback[feedback_before:], [])
        self.assertEqual(self.display.writes, writes_before)
        self.assertTrue(self.agent.runtime.ready)
        self.context.absent = False
        self.agent.tick(13.6)
        self.assertTrue(self.agent.runtime.ready)

    def test_stale_presence_reports_lost_when_grace_expires(self):
        self.context.snapshot = lambda: dict(
            suspended=False, error=None, display_on=True, control_available=False,
            presence_stale=True,
            presence=dict(session_id=1, console_session_id=1, active=True, locked=False),
        )
        self.agent.start()
        self.agent.tick(10.)
        self.context.absent = True
        self.agent.tick(10.6)
        self.agent.tick(13.7)
        self.assertIn((0, False, 'display-unavailable', 'native-session-unavailable'),
                      self.client.feedback)
        self.assertFalse(self.agent.runtime.ready)

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

    def test_authority_proof_retries_a_startup_generation_change(self):
        original_update = self.client.session_update
        changed = False

        def startup_tick(generation, ready, error_code=None, error_detail=None):
            nonlocal changed
            original_update(generation, ready, error_code, error_detail)
            if not changed:
                changed = True
                self.client.state = replace(
                    self.client.state,
                    generation=self.client.state.generation + 1,
                    session_connected=True,
                )

        self.client.session_update = startup_tick
        with patch('panasonic_posture.windows.session.agent.time.sleep'):
            self.agent.start()
            self.agent.tick(10.)
        self.assertEqual(self.created, [True])
        self.assertEqual(
            [generation for generation, ready, _, _ in self.client.feedback if not ready],
            [self.client.state.generation - 1, self.client.state.generation],
        )

    def test_laptop_tick_does_not_wait_on_the_display_lane_for_release_preparation(self):
        self.agent.start()
        with patch.object(self.agent.runtime, 'call_display') as call_display:
            self.agent.tick(10.)
        call_display.assert_not_called()

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
        heartbeats = [
            entry for entry in jobs
            if entry[0] == self.agent._qualification_heartbeat
        ]
        self.assertEqual(len(heartbeats), 1)
        for _ in range(10):
            self.agent.tick(11.)
        self.assertEqual(len([
            entry for entry in jobs
            if entry[0] == self.agent._qualification_heartbeat
        ]), 1)
        self.context.absent = False
        self.agent.tick(13.)
        self.assertTrue(self.agent.runtime.failure_active)
        self.assertEqual(self.display.current, 0)
        function, future = heartbeats[0]
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
            client_factory=lambda available, unattended: self.client,
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
            client_factory=lambda available, unattended: self.client,
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
                patch('panasonic_posture.windows.hardware.convertible_slate.NativeSlateDevice', Denied), \
                self.assertLogs('panasonic_posture.windows.session.agent', level='WARNING') as logged:
            result = build_optional_slate(self.client, Path(directory) / 'display.json')
        self.assertIsNone(result)
        self.assertIn('continuing without CSM', '\n'.join(logged.output))

    def test_display_recovery_is_gated_by_the_authenticated_physical_query(self):
        state = StateSnapshot(hardware=None, hardware_status=HardwareStatus.ABSENT)
        client = SimpleNamespace(get_state=lambda: state)
        with tempfile.TemporaryDirectory() as directory, patch(
            'panasonic_posture.windows.session.display_backend.NativeDisplay'
        ), patch('panasonic_posture.windows.session.screen_session.select_native_display') as display:
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
                patch('panasonic_posture.windows.hardware.convertible_slate.NativeSlateDevice', Device), \
                patch('panasonic_posture.windows.hardware.convertible_slate.SlateNotifier', Notifier):
            result = build_optional_slate(Client(), Path(directory) / 'display.json')
        self.assertIsInstance(created['device'], Device)
        self.assertIsInstance(result, Notifier)
        from panasonic_posture.windows.session.display import SessionHardwareQuery
        self.assertIsInstance(created['query'], SessionHardwareQuery)


class NativeAgentTokenTests(unittest.TestCase):
    def test_rejected_entry_does_not_access_user_state_or_construct_the_agent(self):
        from contextlib import redirect_stderr
        from io import StringIO
        with patch('panasonic_posture.windows.session.agent.sys.platform', 'win32'), \
             patch('panasonic_posture.windows.common.native_preflight.require_native_user',
                   side_effect=PermissionError('fixture not local')) as require, \
             patch('panasonic_posture.windows.common.diagnostics.configure_logging') as logging, \
             patch('panasonic_posture.windows.common.instance.InstanceLock',
                   side_effect=PermissionError('fixture must not acquire a lock')) as lock, \
             patch('panasonic_posture.windows.session.agent.WindowsSessionAgent') as agent, \
             redirect_stderr(StringIO()):
            result = main(['--journal', str(Path(tempfile.gettempdir()) / 'fixture-display.json')])
        self.assertEqual(result, 1)
        require.assert_called_once_with()
        logging.assert_not_called()
        lock.assert_not_called()
        agent.assert_not_called()

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
            with patch('panasonic_posture.windows.session.agent.WindowsSessionAgent', return_value=agent), \
                 patch('panasonic_posture.windows.common.stop_event.NativeStopSignal.open', return_value=signal), \
                 patch('panasonic_posture.windows.common.diagnostics.configure_logging',
                       side_effect=lambda path: events.append('logging')), \
                 patch('panasonic_posture.windows.common.native_preflight.require_native_user',
                       side_effect=lambda: events.append('preflight')), \
                 patch('panasonic_posture.windows.session.agent.sys.platform', 'win32'), \
                 patch('panasonic_posture.windows.session.agent.time.sleep'):
                result = main(['--journal', str(Path(directory) / 'display.json'), '--stop-event', 'fixture'])
        self.assertEqual(result, 0)
        self.assertEqual(events, ['preflight', 'logging', 'start', 'tick', 'restore', 'event-close'])

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
