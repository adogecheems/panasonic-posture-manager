"""Privileged Windows dispatch never trusts identities or receipts in JSON."""
from concurrent.futures import Future
import ctypes as C
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from panasonic_posture.model import HardwareStatus, Mode, Target
from panasonic_posture.service.hardware_monitor import HardwareMonitor
from panasonic_posture.service.ports import SessionOwner
from panasonic_posture.service.settings_store import SettingsStore
from panasonic_posture.windows.service.host import ConsoleIdentity, HardwareHost, CHANNEL, validate_request
from panasonic_posture.windows.ipc.pipe import FILE_FLAG_OVERLAPPED, NativePipes, PipePoll
from panasonic_posture.windows.ipc.frames import ProtocolError
from panasonic_posture.windows.service.ipc import ConsoleGuard, HardwareServer, HardwareConnection, CLIENT_ACCESS
from panasonic_posture.windows.common.security import (
    AuthenticationError, ConsoleAccess, ExtendedInfo, WindowsSecurity, foreground_matches,
)
from panasonic_posture.tasks import InlineExecutor
try:
    from tests.windows_fixture import Simulation
except ModuleNotFoundError:
    from windows_fixture import Simulation

UI = ConsoleIdentity('S-1-5-21-1000', 100, 1, 42, 100000)
AGENT = ConsoleIdentity('S-1-5-21-1000', 100, 1, 43, 100001)


class FixtureSecurity:
    def console_access(self, peer):
        return ConsoleAccess(self.is_active(peer))


class HostFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.simulation = Simulation(
            SettingsStore(Path(directory.name) / 'settings.json'),
            'windows-test-host',
        )
        self.addCleanup(self.simulation.close)
        self.engine = self.simulation.engine
        self.active = True
        self.host = HardwareHost(self.engine, authorize=lambda peer: self.active,
                                 session_process=AGENT)

    def request(self, method, **params):
        return dict(version=1, channel=CHANNEL, id=1, instance=self.host.instance,
                    method=method, params=params)


class HardwareHostTests(HostFixture):
    def test_registry_follows_owned_target_and_physical_handoff(self):
        writer = Mock()
        physical = [False]
        self.host = HardwareHost(self.engine, authorize=lambda peer: self.active,
                                 session_process=AGENT, registry=writer,
                                 physical_state=lambda: physical[0])
        self.engine.revoke_session(self.engine.session_owner)
        self.host.dispatch(UI, self.request('set_mode', mode='tent'))
        writer.apply.assert_not_called()
        def receipt(ready):
            return self.host.dispatch(AGENT, self.request('session_update',
                generation=self.engine.state.generation, ready=ready, unattended=False,
                error_code=None, error_detail=None))
        receipt(False)
        writer.apply.assert_called_with(True)
        self.host.dispatch(UI, self.request('set_mode', mode='laptop'))
        writer.apply.reset_mock()
        self.host.tick()
        writer.apply.assert_not_called()
        receipt(True)
        writer.apply.assert_called_with(False)
        physical[0] = True
        self.host.tick()
        writer.release.assert_called_with(True)
        physical[0] = None
        self.host.tick()
        writer.release.assert_called_with(None)

    def test_registry_failure_withholds_applied_and_is_retried(self):
        self.engine.revoke_session(self.engine.session_owner)
        writer = Mock()
        writer.apply.side_effect = OSError('write denied')
        self.host = HardwareHost(self.engine, authorize=lambda peer: True,
                                 session_process=AGENT, registry=writer, physical_state=lambda: False)
        self.host.dispatch(UI, self.request('set_mode', mode='tent'))
        self.host.dispatch(AGENT, self.request('session_update', generation=self.engine.state.generation,
                           ready=False, error_code=None, error_detail=None, unattended=False))
        reply = self.host.dispatch(UI, self.request('get_state'))
        writer.release.assert_called_with(False)
        self.assertEqual(reply['state']['error_detail'], 'registry-posture')
        self.assertIsNone(reply['state']['applied'])
        writer.apply.side_effect = None
        self.host.tick()
        self.assertIsNone(self.host.state.error_code)

    def test_lock_switches_to_auto_without_releasing_the_registered_session(self):
        self.engine.revoke_session(self.engine.session_owner)
        self.engine.set_mode('tent')
        self.engine.tick()
        self.host.session_authorize = lambda peer: peer == AGENT
        locked = [False]
        self.host.session_locked = lambda: locked[0]
        self.engine.session_update(self.engine.state.generation, True, None, None, AGENT.owner)
        locked[0], self.active = True, False
        self.host.tick()
        self.assertEqual(self.engine.state.manual, 'auto')
        self.assertEqual(self.engine.session_owner, AGENT.owner)
        generation = self.engine.state.generation
        self.host.tick()
        self.assertEqual(self.engine.state.generation, generation)
        reply = self.host.dispatch(AGENT, self.request('session_update', generation=generation,
            ready=True, error_code=None, error_detail=None, unattended=True))
        self.assertTrue(reply['accepted'])
        with self.assertRaisesRegex(Exception, 'access-denied'):
            self.host.dispatch(UI, self.request('get_state'))
        with self.assertRaisesRegex(Exception, 'access-denied'):
            self.host.dispatch(AGENT, self.request('set_mode', mode='tent'))

    def test_recovery_read_is_exact_process_only_and_never_renews_ownership(self):
        self.active = False
        self.host.tick()
        before = self.engine.state.to_dict()
        reply = self.host.recovery_state(AGENT, self.request('get_state'))
        self.assertTrue(reply['accepted'])
        self.assertEqual(reply['state'], before)
        self.assertIsNone(self.engine.session_owner)
        self.assertEqual(self.engine.state.to_dict(), before)
        for peer in (UI, None, ConsoleIdentity(AGENT.sid, AGENT.logon_id, 1, 43, 99999)):
            with self.subTest(peer=peer), self.assertRaisesRegex(Exception, 'access-denied'):
                self.host.recovery_state(peer, self.request('get_state'))
        for method, params in (('set_mode', dict(mode='tent')), ('session_update', dict(
                generation=self.engine.state.generation, ready=True, error_code=None,
                error_detail=None, unattended=False))):
            with self.subTest(method=method), self.assertRaisesRegex(Exception, 'access-denied'):
                self.host.recovery_state(AGENT, self.request(method, **params))
        self.assertEqual(self.engine.state.to_dict(), before)

    def test_control_operations_require_authenticated_ui_and_preserve_pending_result(self):
        calls = []
        self.host.control = lambda peer, enabled: calls.append((peer, enabled)) or enabled
        self.assertFalse(self.host.dispatch(UI, self.request('stop_control'))['accepted'])
        self.assertTrue(self.host.dispatch(UI, self.request('start_control'))['accepted'])
        self.assertEqual(calls, [(UI, False), (UI, True)])
        for peer in (AGENT, None):
            with self.subTest(peer=peer), self.assertRaisesRegex(Exception, 'access-denied'):
                self.host.dispatch(peer, self.request('stop_control'))
        self.active = False
        with self.assertRaisesRegex(Exception, 'access-denied'):
            self.host.dispatch(UI, self.request('stop_control'))
        self.assertEqual(len(calls), 2)

    def test_invalid_physical_source_blocks_receipts_and_revokes_the_owner(self):
        admitted = [True]
        self.host.session_admission = lambda: admitted[0]
        self.engine.revoke_session(self.engine.session_owner)
        self.engine.set_mode('tent')
        self.engine.tick()
        receipt = self.request('session_update', generation=self.engine.state.generation,
            ready=True, error_code=None, error_detail=None, unattended=False)
        reply = self.host.dispatch(AGENT, receipt)
        self.assertTrue(
            reply['accepted'],
            (
                reply['state']['generation'],
                self.engine._controller.generation,
                reply['state']['session_connected'],
                reply['state']['supported'],
            ),
        )
        admitted[0] = False
        with self.assertRaisesRegex(Exception, 'display-unavailable'):
            self.host.dispatch(AGENT, receipt)
        self.host.tick()
        self.assertIsNone(self.engine.session_owner)
        self.assertIsNone(self.engine.state.applied)
        self.assertEqual(self.engine._controller.manual, Mode.AUTO)
        admitted[0] = True
        fresh = self.request('session_update', generation=self.engine.state.generation,
            ready=True, error_code=None, error_detail=None, unattended=False)
        self.assertTrue(self.host.dispatch(AGENT, fresh)['accepted'])

    def test_unobserved_physical_source_allows_only_a_not_ready_heartbeat(self):
        admitted = [False]
        self.host.session_admission = lambda: admitted[0]
        self.engine._hardware_monitor = HardwareMonitor(
            lambda: (None, HardwareStatus.ABSENT)
        )
        self.engine.revoke_session(self.engine.session_owner)
        heartbeat = self.request('session_update', generation=self.engine.state.generation,
            ready=False, error_code=None, error_detail=None, unattended=False)
        reply = self.host.dispatch(AGENT, heartbeat)
        self.assertTrue(reply['accepted'])
        self.assertTrue(reply['state']['session_connected'])
        self.assertIsNone(reply['state']['hardware'])
        self.assertEqual(reply['state']['target']['mode'], 'unavailable')
        self.assertIsNone(self.engine.state.applied)

        ready = self.request('session_update', generation=self.engine.state.generation,
            ready=True, error_code=None, error_detail=None, unattended=False)
        with self.assertRaisesRegex(Exception, 'display-unavailable'):
            self.host.dispatch(AGENT, ready)
        self.assertIsNone(self.engine.state.applied)

    def test_ui_can_choose_mode_but_cannot_submit_display_receipts(self):
        self.host.dispatch(UI, self.request('set_mode', mode='tent'))
        self.engine.tick()
        self.assertIsNone(self.engine.state.applied)
        self.assertFalse(self.simulation.actuator.running)
        receipt = self.request('session_update', generation=self.engine.state.generation,
            ready=True, error_code=None, error_detail=None, unattended=False)
        with self.assertRaisesRegex(Exception, 'access-denied'):
            self.host.dispatch(UI, receipt)
        self.assertIsNone(self.engine.state.applied)
        self.engine.revoke_session(self.engine.session_owner)
        reply = self.host.dispatch(AGENT, receipt)
        self.assertTrue(reply['accepted'])
        self.engine.tick()
        self.assertEqual(self.engine.state.applied.mode, 'tent')
        self.host.disconnected(AGENT)
        self.assertIsNone(self.engine.state.applied)
        self.assertFalse(self.simulation.actuator.running)

    def test_lock_switch_or_disconnect_revokes_only_registered_owner(self):
        self.engine.revoke_session(self.engine.session_owner)
        self.engine.session_update(self.engine.state.generation, True, None, None, AGENT.owner)
        self.host.disconnected(UI)
        self.assertEqual(self.engine.session_owner, AGENT.owner)
        self.host.disconnected(AGENT)
        self.assertIsNone(self.engine.session_owner)
        self.assertIsNone(self.engine.state.applied)
        self.engine.session_update(self.engine.state.generation, True, None, None, AGENT.owner)
        self.active = False
        self.host.tick()
        self.assertIsNone(self.engine.session_owner)
        self.assertFalse(self.simulation.actuator.running)
        with self.assertRaisesRegex(Exception, 'access-denied'):
            self.host.dispatch(UI, self.request('get_state'))

    def test_lock_resets_stale_posture_even_after_pipe_disconnect(self):
        self.engine.set_mode('tent')
        self.engine.tick()
        generation = self.engine.state.generation
        self.host.disconnected(AGENT)
        self.active = False

        self.host.tick()

        self.assertGreater(self.engine.state.generation, generation)
        self.assertEqual(self.engine._controller.manual, Mode.AUTO)
        self.assertEqual(self.engine._controller.target, Target())
        reset_generation = self.engine.state.generation
        self.host.tick()
        self.assertEqual(self.engine.state.generation, reset_generation)

    def test_stale_generation_and_process_reuse_cannot_acknowledge(self):
        reused = ConsoleIdentity(AGENT.sid, AGENT.logon_id, AGENT.session_id,
                                 AGENT.process_id, AGENT.created_at + 1)
        receipt = self.request('session_update', generation=self.engine.state.generation - 1,
            ready=True, error_code=None, error_detail=None, unattended=False)
        self.assertFalse(self.host.dispatch(AGENT, receipt)['accepted'])
        with self.assertRaisesRegex(Exception, 'access-denied'):
            self.host.dispatch(reused, receipt)

    def test_strict_types_and_instance_are_checked_before_dispatch(self):
        original = self.engine.state.to_dict()
        for changes in ({'id': True}, {'instance': 'old'}, {'channel': 'software'},
                        {'params': {'mode': []}}, {'owner': AGENT.owner},
                        {'method': 'open_device', 'params': {'path': 'arbitrary'}}):
            request = self.request('set_mode', mode='tent')
            request.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ProtocolError):
                self.host.dispatch(UI, request)
        self.assertEqual(self.engine.state.to_dict(), original)
        for params in (dict(generation=True), dict(error_code=[]), dict(ready=1),
                       dict(unattended=1), dict(error_detail='x' * 513)):
            request = self.request('session_update', generation=self.engine.state.generation,
                ready=False, error_code=None, error_detail=None, unattended=False)
            request['params'].update(params)
            with self.subTest(params=params), self.assertRaises(ProtocolError):
                self.host.dispatch(AGENT, request)

    def test_unrelated_owner_cannot_be_revoked(self):
        owner = self.engine.session_owner
        self.engine.revoke_session(SessionOwner('another-platform', 'foreign'))
        self.assertEqual(self.engine.session_owner, owner)


class SecurityTests(unittest.TestCase):
    def test_locked_recovery_keeps_native_console_logon_and_process_checks(self):
        info = ExtendedInfo()
        info.level, info.data.session, info.data.state, info.data.flags = 1, 1, 0, 0
        security = object.__new__(WindowsSecurity)
        console = [AGENT.sid, AGENT.logon_id, AGENT.session_id]
        current = [AGENT]
        session = [1]
        def query_token(session, token):
            token._obj.value = 12
            return True
        def query_info(server, session, kind, buffer, size):
            buffer._obj.value = C.addressof(info)
            size._obj.value = C.sizeof(info)
            return True
        security.kernel = SimpleNamespace(WTSGetActiveConsoleSessionId=lambda: session[0],
                                          CloseHandle=lambda handle: None)
        security.wts = SimpleNamespace(WTSQueryUserToken=query_token,
            WTSQuerySessionInformationW=query_info, WTSFreeMemory=lambda buffer: None)
        security.process_identity = lambda pid: current[0]
        security.token_identity = lambda token: tuple(console)
        self.assertFalse(security.is_active(AGENT))
        self.assertTrue(security.is_active(AGENT, allow_locked=True))
        for field, value in (('flags', -1), ('state', 1), ('session', 2)):
            with self.subTest(field=field):
                previous = getattr(info.data, field)
                setattr(info.data, field, value)
                self.assertFalse(security.is_active(AGENT, allow_locked=True))
                setattr(info.data, field, previous)
        console[1] = 999
        self.assertFalse(security.is_active(AGENT, allow_locked=True))
        console[1] = AGENT.logon_id
        current[0] = UI
        self.assertFalse(security.is_active(AGENT, allow_locked=True))
        current[0] = AGENT
        session[0] = 2
        self.assertFalse(security.is_active(AGENT, allow_locked=True))

    def test_peer_relaxes_lock_only_for_exact_registered_identity(self):
        security = object.__new__(WindowsSecurity)
        def pid(handle, output):
            output._obj.value = AGENT.process_id
            return True
        security.kernel = SimpleNamespace(GetCurrentThread=lambda: 1,
            GetNamedPipeClientProcessId=pid, CloseHandle=lambda handle: None)
        security.advapi = SimpleNamespace(ImpersonateNamedPipeClient=lambda pipe: True,
            OpenThreadToken=lambda *args: True, RevertToSelf=lambda: True)
        security.token_identity = lambda token: (AGENT.sid, AGENT.logon_id, AGENT.session_id)
        security.process_identity = lambda pid: AGENT
        security.is_active = lambda peer, allow_locked=False: allow_locked
        self.assertEqual(security.peer(1, recovery_process=AGENT), AGENT)
        for registered in (None, UI, ConsoleIdentity(AGENT.sid, 100, 1, 43, 99999)):
            with self.subTest(registered=registered), self.assertRaises(AuthenticationError):
                security.peer(1, recovery_process=registered)
        security.token_identity = lambda token: (AGENT.sid, 999, AGENT.session_id)
        with self.assertRaises(AuthenticationError):
            security.peer(1, recovery_process=AGENT)

    def test_agent_replacement_ignores_old_auth_results_without_spawning_extra_work(self):
        jobs, peers = [], []
        now = [10.]
        class Executor:
            def submit(self, function):
                future = Future()
                jobs.append((function, future))
                return future
        def check(peer):
            peers.append(peer)
            return True
        replacement = ConsoleIdentity(AGENT.sid, 101, 2, 44, 100002)
        guard = ConsoleGuard(check, AGENT, clock=lambda: now[0], executor=Executor())
        self.addCleanup(guard.close)
        guard.remember(AGENT, 10.)
        guard.tick()
        guard.set_agent(replacement)
        for _ in range(20):
            guard.tick()
        self.assertEqual(len(jobs), 1)
        self.assertFalse(guard.allow(AGENT))
        function, future = jobs[0]
        future.set_result(function())
        guard.tick()
        self.assertEqual(peers, [AGENT])
        self.assertFalse(guard.allow(replacement))
        self.assertEqual(len(jobs), 2)
        function, future = jobs[1]
        future.set_result(function())
        guard.tick()
        self.assertEqual(peers, [AGENT, replacement])
        self.assertTrue(guard.allow(replacement))

    def test_requires_same_logon_unlocked_local_console(self):
        console = UI.sid, UI.logon_id, UI.session_id
        self.assertTrue(foreground_matches(UI, console, active=True, unlocked=True))
        for args in ((console, False, True), (console, True, False),
                     ((UI.sid, 999, UI.session_id), True, True),
                     ((UI.sid, UI.logon_id, 2), True, True),
                     (('S-1-5-21-1001', UI.logon_id, 1), True, True)):
            self.assertFalse(foreground_matches(UI, args[0], active=args[1], unlocked=args[2]))
        # FILE_APPEND_DATA aliases FILE_CREATE_PIPE_INSTANCE for named pipes.
        self.assertEqual(CLIENT_ACCESS & 4, 0)

    def test_slow_authentication_expires_without_blocking_or_extending_a_lease(self):
        now = [10.]
        future = Future()
        class Executor:
            def submit(self, fn):
                return future
        guard = ConsoleGuard(lambda peer: True, AGENT, clock=lambda: now[0], executor=Executor())
        guard.remember(AGENT, now[0])
        guard.tick()
        self.assertTrue(guard.allow(AGENT))
        now[0] += 1.1
        self.assertFalse(guard.allow(AGENT))
        future.set_result((10., True))
        guard.tick()
        self.assertFalse(guard.allow(AGENT))


class PipeTests(HostFixture):
    def test_locked_agent_can_renew_but_ui_and_other_methods_cannot_authenticate(self):
        server, _, _ = self.server()
        self.engine.revoke_session(self.engine.session_owner)
        server.security.console_access = lambda peer: ConsoleAccess(True, True)
        server.guard.check = server.security.console_access
        server.guard.tick()
        server.guard.tick()
        server.host.tick()
        self.assertTrue(server.guard.allow(AGENT))
        self.assertFalse(server.guard.allow_ui(AGENT))
        peer = [AGENT]
        def authenticate(handle, *, recovery_process=None):
            if peer[0] != recovery_process:
                raise AuthenticationError('locked fixture')
            return peer[0]
        server.security.peer = authenticate
        request = self.request('session_update', generation=self.engine.state.generation,
                               ready=True, error_code=None, error_detail=None, unattended=True)
        server.pipes.read = lambda handle: PipePoll.ready(request)
        slot = server.slots[0]
        server._read_request(slot)
        self.assertTrue(server._finish_authentication(slot))
        self.assertTrue(slot.reply['accepted'])
        self.assertEqual(self.engine.session_owner, AGENT.owner)
        self.assertFalse(server.guard.allow_ui(AGENT))
        for identity, method, params in ((UI, 'get_state', {}), (UI, 'session_update', request['params']),
                (AGENT, 'set_mode', dict(mode='tent')), (AGENT, 'start_control', {})):
            with self.subTest(identity=identity, method=method):
                peer[0] = identity
                slot.peer = None
                request = self.request(method, **params)
                server._read_request(slot)
                with self.assertRaises(AuthenticationError):
                    server._finish_authentication(slot)

    def test_recovery_read_does_not_authorize_receipts_or_break_ui_reads(self):
        server, _, _ = self.server()
        peer, locked = [AGENT], [True]
        def authenticate(handle, *, recovery_process=None):
            if locked[0] and peer[0] != recovery_process:
                raise AuthenticationError('locked fixture')
            return peer[0]
        server.security.peer = authenticate
        slot = server.slots[0]
        server.pipes.read = lambda handle: PipePoll.ready(self.request('get_state'))
        server._read_request(slot)
        self.assertTrue(server._finish_authentication(slot))
        self.assertTrue(slot.reply['accepted'])
        self.assertFalse(server.guard.allow(AGENT))
        with self.assertRaisesRegex(Exception, 'access-denied'):
            server.host.dispatch(AGENT, self.request('session_update', generation=1,
                ready=True, error_code=None, error_detail=None, unattended=False))
        slot.peer, slot.serial = None, 0
        peer[0] = UI
        server._read_request(slot)
        with self.assertRaises(AuthenticationError):
            server._finish_authentication(slot)
        locked[0] = False
        server._read_request(slot)
        self.assertTrue(server._finish_authentication(slot))
        self.assertTrue(slot.reply['accepted'])

    def test_recovery_read_rechecks_registration_after_authentication(self):
        server, _, _ = self.server()
        server.security.peer = lambda handle, **kwargs: AGENT
        slot = server.slots[0]
        server.pipes.read = lambda handle: PipePoll.ready(self.request('get_state'))
        server._read_request(slot)
        server.host.session_process = UI
        self.assertTrue(server._finish_authentication(slot))
        self.assertFalse(slot.reply['accepted'])
        self.assertEqual(slot.reply['error'], 'access-denied')
        self.assertFalse(server.guard.allow(AGENT))

    def test_slow_recovery_authentication_cannot_use_an_expired_identity_check(self):
        for elapsed in (-.1, 1., 5.):
            with self.subTest(elapsed=elapsed):
                server, _, _ = self.server()
                server.security.peer = lambda handle, **kwargs: AGENT
                now = [10.]
                server.clock = lambda: now[0]
                slot = server.slots[0]
                server.pipes.read = lambda handle: PipePoll.ready(self.request('get_state'))
                server._read_request(slot)
                now[0] += elapsed
                self.assertTrue(server._finish_authentication(slot))
                self.assertFalse(slot.reply['accepted'])
                self.assertEqual(slot.reply['error'], 'access-denied')
                self.assertFalse(server.guard.allow(AGENT))

    def test_cancelled_slot_stays_retired_while_other_service_ticks_continue(self):
        server, _, _ = self.server()
        slot = server.slots[0]
        complete, accepted = [False], []
        server.pipes.reset = lambda handle: complete[0]
        server.pipes.accept = lambda handle: accepted.append(handle) or PipePoll.pending()
        slot.connected, slot.peer = True, AGENT
        ticks = []
        self.engine.tick = lambda: ticks.append(True)
        server._reset(slot)
        self.assertTrue(slot.retired)
        for _ in range(4): server.poll()
        self.assertEqual(len(ticks), 4)
        self.assertNotIn(slot.handle, accepted)
        complete[0] = True
        server.poll()
        self.assertFalse(slot.retired)
        self.assertFalse(slot.connected)
        server.poll()
        self.assertIn(slot.handle, accepted)

    def test_registration_preserves_a_validated_ui_with_authentication_in_flight(self):
        server, resets, _ = self.server()
        slot = server.slots[0]
        pending = Future()
        slot.peer, slot.connected, slot.pending = UI, True, pending
        slot.request = self.request('get_state')
        server.register_session_process(None)
        self.assertTrue(slot.connected)
        self.assertFalse(slot.retired)
        self.assertNotIn(slot.handle, resets)
        pending.set_result((UI, server.clock()))
        self.assertTrue(server._finish_authentication(slot))
        self.assertTrue(slot.reply['accepted'])

    def server(self):
        resets, replies = [], []
        handles = iter(range(4))
        class Pipes:
            def create(self, name, *, first): return next(handles)
            def accept(self, handle): return PipePoll.pending()
            def close(self, handle): pass
            def reset(self, handle): resets.append(handle)
            def write(self, handle, value):
                replies.append(value)
                return PipePoll.ready()
        class Security(FixtureSecurity):
            def require_system(self): pass
            def is_active(self, peer): return True
        server = HardwareServer(self.engine, session_process=AGENT, security=Security(), pipes=Pipes(),
                                executor=InlineExecutor(), guard_executor=InlineExecutor())
        self.addCleanup(server.close)
        return server, resets, replies

    def test_trusted_registration_revokes_previous_owner_and_receipt_qualification(self):
        server, resets, _ = self.server()
        self.engine.revoke_session(self.engine.session_owner)
        self.engine.session_update(self.engine.state.generation, True, None, None, AGENT.owner)
        server.guard.remember(AGENT, server.clock())
        replacement = ConsoleIdentity(AGENT.sid, AGENT.logon_id, AGENT.session_id, 44, 100002)
        server.register_session_process(replacement)
        self.assertIsNone(self.engine.session_owner)
        self.assertIsNone(self.engine.state.applied)
        self.assertEqual(server.host.session_process, replacement)
        self.assertFalse(server.guard.allow(AGENT))
        self.assertEqual(resets, [0, 1, 2, 3])
        request = self.request('session_update', generation=self.engine.state.generation, ready=False,
                               error_code=None, error_detail=None, unattended=False)
        with self.assertRaisesRegex(Exception, 'access-denied'):
            server.host.dispatch(AGENT, request)
        server.guard.remember(replacement, server.clock())
        self.assertTrue(server.host.dispatch(replacement, request)['accepted'])
        server.register_session_process(None)
        self.assertIsNone(self.engine.session_owner)
        self.assertIsNone(server.host.session_process)

    def test_registration_discards_pending_pipe_auth_before_it_can_dispatch(self):
        server, resets, replies = self.server()
        future = Future()
        slot = server.slots[0]
        slot.peer, slot.connected, slot.pending = AGENT, True, future
        slot.request = self.request('set_mode', mode='tent')
        replacement = ConsoleIdentity(AGENT.sid, AGENT.logon_id, AGENT.session_id, 44, 100002)
        server.register_session_process(replacement)
        self.assertTrue(slot.retired)
        self.assertFalse(future.done())
        self.assertEqual(len(resets), 3)
        future.set_result((AGENT, server.clock()))
        server.poll()
        self.assertFalse(slot.retired)
        self.assertFalse(replies)
        self.assertEqual(resets[-1], 0)

    def test_stalled_auth_jobs_stay_bounded_while_engine_ticks_continue(self):
        now, jobs, ticks = [10.], [], []
        class Executor:
            def submit(self, fn):
                future = Future()
                jobs.append(future)
                return future
        class Pipes:
            handles = iter(range(4))
            def create(self, name, *, first):
                return next(self.handles)
            def accept(self, handle):
                return PipePoll.ready(True)
            def read(self, handle):
                return PipePoll.ready({'version': 1})
            def close(self, handle):
                pass
        class Security(FixtureSecurity):
            def require_system(self):
                pass
            def is_active(self, peer):
                return True
        self.engine.tick = lambda: ticks.append(True)
        server = HardwareServer(self.engine, security=Security(), pipes=Pipes(),
            clock=lambda: now[0], executor=Executor(), guard_executor=InlineExecutor())
        self.addCleanup(server.close)
        server.poll()
        self.assertEqual(len(jobs), 4)
        now[0] += 6
        for _ in range(100):
            server.poll()
        self.assertEqual(len(jobs), 4)
        self.assertTrue(all(slot.retired for slot in server.slots))
        self.assertEqual(len(ticks), 101)

    def test_pending_response_write_never_blocks_the_owner_loop(self):
        writes, ticks = [], []

        class Pipes:
            handles = iter(range(4))

            def create(self, name, *, first):
                return next(self.handles)

            def accept(self, handle):
                return PipePoll.pending()

            def write(self, handle, value):
                writes.append(value)
                return PipePoll.pending() if len(writes) == 1 else PipePoll.ready()

            def close(self, handle):
                pass

            def reset(self, handle):
                pass

        class Security(FixtureSecurity):
            def require_system(self):
                pass

            def is_active(self, peer):
                return True

        self.engine.tick = lambda: ticks.append(True)
        server = HardwareServer(
            self.engine, security=Security(), pipes=Pipes(), executor=InlineExecutor(),
            guard_executor=InlineExecutor(),
        )
        self.addCleanup(server.close)
        slot = server.slots[0]
        slot.connected, slot.touched = True, server.clock()
        slot.reply = dict(version=1, channel=CHANNEL, type='response', id=1)
        server.poll()
        self.assertEqual(len(writes), 1)
        self.assertIsNotNone(slot.reply)
        self.assertEqual(len(ticks), 1)
        server.poll()
        self.assertEqual(len(writes), 2)
        self.assertIsNone(slot.reply)
        self.assertEqual(len(ticks), 2)

    def test_close_never_unlinks_a_pipe_while_authentication_still_owns_it(self):
        now, jobs, closed = [10.], [], []

        class Executor:
            def submit(self, fn):
                del fn
                future = Future()
                jobs.append(future)
                return future

        class Pipes:
            handles = iter(range(4))
            def create(self, name, *, first): return next(self.handles)
            def accept(self, handle): return PipePoll.ready(True)
            def read(self, handle): return PipePoll.ready({'version': 1})
            def close(self, handle): closed.append(handle)
            def reset(self, handle): pass

        class Security(FixtureSecurity):
            def require_system(self): pass
            def is_active(self, peer): return True

        server = HardwareServer(self.engine, security=Security(), pipes=Pipes(),
            clock=lambda: now[0], executor=Executor(), guard_executor=InlineExecutor())
        server.poll()
        self.assertEqual(len(jobs), 4)
        server.close()
        self.assertTrue(server.closed)
        self.assertEqual(closed, [])
        for future in jobs:
            future.set_result((AGENT, now[0]))
        self.assertEqual(sorted(closed), list(range(4)))

    def test_fake_transport_dispatches_after_os_auth_and_rejects_replays(self):
        requests, responses, resets = {}, [], []
        class Pipes:
            def create(self, name, *, first):
                return len(requests)
            def accept(self, handle):
                return PipePoll.ready(True)
            def read(self, handle):
                request = requests.pop(handle, None)
                return PipePoll.ready(request) if request is not None else PipePoll.idle()
            def write(self, handle, reply):
                responses.append(reply)
                return PipePoll.ready()
            def reset(self, handle):
                resets.append(handle)
            def close(self, handle):
                pass
        # Use distinct fixed handles; only one receives fixture messages.
        handles = iter(range(4))
        pipes = Pipes()
        pipes.create = lambda name, first: next(handles)
        class Security(FixtureSecurity):
            def require_system(self):
                pass
            def peer(self, handle):
                return UI
            def is_active(self, peer):
                return True
        server = HardwareServer(self.engine, security=Security(), pipes=pipes,
            executor=InlineExecutor(), guard_executor=InlineExecutor())
        self.addCleanup(server.close)
        requests[0] = self.request('get_state')
        server.poll()
        self.assertEqual(responses, [])
        server.poll()
        self.assertEqual(responses[0]['id'], 1)
        requests[0] = self.request('get_state')
        server.poll()
        server.poll()
        self.assertEqual(len(responses), 1)
        self.assertIn(0, resets)

    def test_client_invalid_response_closes_without_replaying_the_request(self):
        sent, closed = [], []
        class Pipes:
            def connect(self, name):
                return 1
            def write(self, handle, value):
                sent.append(value)
                return PipePoll.ready()
            def read(self, handle):
                return PipePoll.ready({'version': 1, 'channel': 'wrong'})
            def close(self, handle):
                closed.append(handle)
        client = HardwareConnection(pipes=Pipes())
        with self.assertRaises(ProtocolError):
            client.request('get_state', {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(closed, [1])


class NativePipeReadTests(unittest.TestCase):
    def test_close_waits_off_thread_and_retains_native_buffers_until_completion(self):
        from panasonic_posture.windows.ipc.pipe import _Operation, Overlapped
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        closed, waits = [], []
        owner = threading.get_ident()
        class Kernel:
            def CancelIoEx(self, *args): return 1
            def GetOverlappedResult(self, handle, overlapped, transferred, wait):
                waits.append((threading.get_ident(), wait))
                if wait:
                    entered.set()
                    release.wait(2)
                    return 1
                C.set_last_error(996)
                return 0
            def CloseHandle(self, handle):
                closed.append(handle)
                if handle == 1: finished.set()
                return 1
        pipes = object.__new__(NativePipes)
        pipes.kernel, pipes._lock = Kernel(), threading.Lock()
        operation = _Operation(100, Overlapped(event=100), buffer=b'pending')
        pipes._operations = {(1, 'write'): operation}
        try:
            pipes.close(1)
            self.assertTrue(entered.wait(1))
            self.assertIs(pipes._operations[(1, 'write')], operation)
            self.assertEqual(closed, [])
            self.assertTrue(all(thread != owner for thread, wait in waits if wait))
        finally:
            release.set()
            self.assertTrue(finished.wait(2))
        self.assertEqual(closed, [100, 1])
        self.assertEqual(pipes._operations, {})

    def test_reset_retains_pending_io_and_never_waits_for_cancellation(self):
        from panasonic_posture.windows.ipc.pipe import _Operation, Overlapped
        calls, closed = [], []
        class Kernel:
            complete = False
            def CancelIoEx(self, *args): calls.append('cancel'); return 1
            def GetOverlappedResult(self, handle, overlapped, transferred, wait):
                self_test.assertFalse(wait)
                C.set_last_error(995 if self.complete else 996)
                return 0
            def CloseHandle(self, handle): closed.append(handle); return 1
            def DisconnectNamedPipe(self, handle): calls.append('disconnect'); return 1
        self_test = self
        pipes = object.__new__(NativePipes)
        pipes.kernel, pipes._lock = Kernel(), threading.Lock()
        operation = _Operation(100, Overlapped(event=100), buffer=b'pending')
        pipes._operations = {(1, 'write'): operation}
        for _ in range(3):
            self.assertFalse(pipes.reset(1))
            self.assertIs(pipes._operations[(1, 'write')], operation)
            self.assertEqual(closed, [])
        self.assertEqual(calls, ['cancel'])
        pipes.kernel.complete = True
        self.assertTrue(pipes.reset(1))
        self.assertEqual(pipes._operations, {})
        self.assertEqual(closed, [100])
        self.assertEqual(calls, ['cancel', 'disconnect'])

    def setUp(self):
        # The injected kernel and its last-error slot form one fake native API.
        self.last_error = 0
        def set_error(value):
            self.last_error = value
        for name, function in (
            ('set_last_error', set_error),
            ('get_last_error', lambda: self.last_error),
        ):
            replacement = patch.object(C, name, function, create=True)
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_server_and_client_handles_request_overlapped_io(self):
        calls = []

        class Kernel:
            def CreateNamedPipeW(self, *args):
                calls.append(('server', args))
                return 10

            def CreateFileW(self, *args):
                calls.append(('client', args))
                return 11

            def SetNamedPipeHandleState(self, *args):
                del args
                return 1

            def LocalFree(self, descriptor):
                del descriptor

        class Advapi:
            @staticmethod
            def ConvertStringSecurityDescriptorToSecurityDescriptorW(*args):
                del args
                return 1

        class Security:
            kernel = Kernel()
            advapi = Advapi()

            def require_system(self):
                pass

            def require_system_pipe(self, handle):
                del handle

            @staticmethod
            def _check(value):
                return value

        pipes = object.__new__(NativePipes)
        pipes.security = Security()
        pipes.kernel = pipes.security.kernel
        pipes._operations = {}
        pipes._lock = threading.Lock()
        self.assertEqual(pipes.create(r'\\.\pipe\fixture', first=True), 10)
        open_mode = calls[0][1][1]
        pipe_mode = calls[0][1][2]
        self.assertTrue(open_mode & FILE_FLAG_OVERLAPPED)
        self.assertEqual(pipe_mode, 4 | 2 | 8)
        self.assertFalse(pipe_mode & 1, 'overlapped I/O requires blocking pipe mode')
        self.assertEqual(pipes.connect(r'\\.\pipe\fixture'), 11)
        client_flags = calls[1][1][5]
        self.assertTrue(client_flags & FILE_FLAG_OVERLAPPED)

    def pipes(self, *, connected, available=0, error=0):
        reads = []
        class Kernel:
            def PeekNamedPipe(self, handle, buffer, size, read, count, overlapped):
                del handle, buffer, size, read, overlapped
                C.cast(count, C.POINTER(C.c_uint32))[0] = available
                if not connected:
                    C.set_last_error(error)
                    return 0
                return 1
            def ReadFile(self, *args):
                reads.append(args)
                return 0
        pipes = object.__new__(NativePipes)
        pipes.kernel = Kernel()
        pipes._operations = {}
        pipes._lock = threading.Lock()
        return pipes, reads

    def test_empty_connected_pipe_is_idle_without_a_read(self):
        pipes, reads = self.pipes(connected=True)
        self.assertEqual(pipes.read(1), PipePoll.idle())
        self.assertEqual(reads, [])

    def test_broken_pipe_is_reported_as_disconnected_instead_of_idle(self):
        pipes, reads = self.pipes(connected=False, error=109)
        with self.assertRaises(OSError) as caught:
            pipes.read(1)
        self.assertEqual(caught.exception.errno, 109)
        self.assertEqual(reads, [])

    def test_response_write_is_overlapped_and_polls_without_blocking(self):
        calls = []
        events = []

        class Kernel:
            expected = 0
            waits = 0

            def CreateEventW(self, *args):
                calls.append(('event', args))
                return 100

            def WriteFile(self, handle, buffer, size, written, overlapped):
                calls.append(('write', handle, size, overlapped))
                self.expected = size
                C.set_last_error(997)
                return 0

            def WaitForSingleObject(self, event, timeout):
                calls.append(('wait', event, timeout))
                self.waits += 1
                return 258 if self.waits == 1 else 0

            def GetOverlappedResult(self, handle, overlapped, transferred, wait):
                calls.append(('result', handle, wait))
                C.cast(transferred, C.POINTER(C.c_uint32))[0] = self.expected
                return 1

            def CloseHandle(self, handle):
                events.append(handle)
                return 1

        pipes = object.__new__(NativePipes)
        pipes.kernel = Kernel()
        pipes._operations = {}
        pipes._lock = threading.Lock()
        first = pipes.write(1, {'ok': 1})
        self.assertEqual(first, PipePoll.pending())
        write = next(call for call in calls if call[0] == 'write')
        self.assertIsNotNone(write[3])
        self.assertNotEqual(write[3], 0)
        self.assertEqual(events, [])
        self.assertEqual(pipes.write(1, {'ok': 1}), PipePoll.ready())
        self.assertEqual(events, [100])
