"""Privileged Windows dispatch never trusts identities or receipts in JSON."""
from concurrent.futures import Future
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.service.ports import SessionOwner
from panasonic_posture.windows.backend import Backend
from panasonic_posture.windows.hardware_host import ConsoleIdentity, HardwareHost, CHANNEL, validate_request
from panasonic_posture.windows.protocol import ProtocolError
from panasonic_posture.windows.hardware_ipc import ConsoleGuard, HardwareServer, HardwareConnection, CLIENT_ACCESS
from panasonic_posture.windows.security import foreground_matches
from panasonic_posture.tasks import InlineExecutor

UI = ConsoleIdentity('S-1-5-21-1000', 100, 1, 42, 100000)
AGENT = ConsoleIdentity('S-1-5-21-1000', 100, 1, 43, 100001)


class HostFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.backend = Backend(Path(directory.name)/'settings.json', simulate=True)
        self.addCleanup(self.backend.close)
        self.engine = self.backend.simulation.engine
        self.active = True
        self.host = HardwareHost(self.engine, authorize=lambda peer: self.active,
                                 session_process=AGENT)

    def request(self, method, **params):
        return dict(version=1, channel=CHANNEL, id=1, instance=self.host.instance,
                    method=method, params=params)


class HardwareHostTests(HostFixture):
    def test_ui_can_choose_mode_but_cannot_submit_display_receipts(self):
        self.host.dispatch(UI, self.request('set_mode', mode='tent'))
        self.engine.tick()
        self.assertIsNone(self.engine.state.applied)
        self.assertFalse(self.backend.simulation.actuator.running)
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
        self.assertFalse(self.backend.simulation.actuator.running)

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
        self.assertFalse(self.backend.simulation.actuator.running)
        with self.assertRaisesRegex(Exception, 'access-denied'):
            self.host.dispatch(UI, self.request('get_state'))

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
                       dict(ready=True, unattended=True), dict(error_detail='x' * 513)):
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
    def server(self):
        resets, replies = [], []
        handles = iter(range(4))
        class Pipes:
            def create(self, name, *, first): return next(handles)
            def accept(self, handle): return False
            def close(self, handle): pass
            def reset(self, handle): resets.append(handle)
            def write(self, handle, value): replies.append(value)
        class Security:
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
                return True
            def read(self, handle):
                return {'version': 1}
            def close(self, handle):
                pass
        class Security:
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

    def test_fake_transport_dispatches_after_os_auth_and_rejects_replays(self):
        requests, responses, resets = {}, [], []
        class Pipes:
            def create(self, name, *, first):
                return len(requests)
            def accept(self, handle):
                return True
            def read(self, handle):
                return requests.pop(handle, None)
            def write(self, handle, reply):
                responses.append(reply)
            def reset(self, handle):
                resets.append(handle)
            def close(self, handle):
                pass
        # Use distinct fixed handles; only one receives fixture messages.
        handles = iter(range(4))
        pipes = Pipes()
        pipes.create = lambda name, first: next(handles)
        class Security:
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
            def read(self, handle):
                return {'version': 1, 'channel': 'wrong'}
            def close(self, handle):
                closed.append(handle)
        client = HardwareConnection(pipes=Pipes())
        with self.assertRaises(ProtocolError):
            client.request('get_state', {})
        self.assertEqual(len(sent), 1)
        self.assertEqual(closed, [1])
