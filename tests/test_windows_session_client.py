"""Native session RPC: lifecycle, independent workers and stale receipts."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest

from panasonic_posture.model import StateSnapshot
from panasonic_posture.controller import Controller
from panasonic_posture.model import DeviceInfo, HardwareStatus, SensorInfo, SensorReading
from panasonic_posture.presentation import PosePresentation
from panasonic_posture.service.engine import Engine, ServiceError as EngineError
from panasonic_posture.session.journal import Journal
from panasonic_posture.session.runtime import SessionRuntime
from panasonic_posture.session.failures import ServiceError, FailureKind
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.display import Panel, WindowsDisplay
from panasonic_posture.windows.hardware_ipc import (
    HardwareConnection, HardwareOperationError, validated_hardware_state, PipePoll,
)
from panasonic_posture.windows.hardware_host import CHANNEL, ConsoleIdentity, HardwareHost
from panasonic_posture.windows.simulation import SimulatedInput
from panasonic_posture.windows.session_client import WindowsSessionClient
from panasonic_posture.windows.session_agent import WindowsSessionAgent
from panasonic_posture.windows.protocol import ProtocolError


def state(instance='native-one'):
    return StateSnapshot(instance=instance, sensor_source='windows-winrt', hardware=False).to_dict()


class Connection:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or state()
        self.calls = []
        self.closed = False
        self.error = None
    def request(self, method, params):
        self.calls.append((method, params))
        if self.error:
            raise self.error
        return dict(instance=self.snapshot['instance'], accepted=True, state=self.snapshot)
    def close(self):
        self.closed = True


class SessionClientTests(unittest.TestCase):
    def client(self, connection=None, **kwargs):
        connection = connection or Connection()
        client = WindowsSessionClient(factory=lambda: connection, **kwargs)
        self.addCleanup(client.close)
        return client, connection

    def test_handshake_precedes_writes_and_empty_runtime_errors_become_null(self):
        client, connection = self.client()
        client.session_update(7, False, '', '')
        self.assertEqual(connection.calls, [('get_state', {}), ('session_update', dict(
            generation=7, ready=False, error_code=None, error_detail=None, unattended=False))])
        self.assertEqual(client.get_state().instance, 'native-one')
        self.assertEqual(len(connection.calls), 3)

    def test_unattended_context_never_claims_display_ready(self):
        client, connection = self.client(unattended=lambda: True)
        client.session_update(1, True)
        self.assertEqual(connection.calls[-1][1]['unattended'], True)
        self.assertEqual(connection.calls[-1][1]['ready'], False)
        self.assertIsNone(connection.calls[-1][1]['error_code'])

    def test_lost_control_qualification_explicitly_revokes_input_preparation(self):
        client, connection = self.client(unavailable=lambda: True)
        client.session_update(1, True)
        receipt = connection.calls[-1][1]
        self.assertFalse(receipt['ready'])
        self.assertEqual(receipt['error_code'], 'display-unavailable')
        self.assertFalse(receipt['unattended'])

    def test_invalid_runtime_feedback_is_not_transmitted(self):
        client, connection = self.client()
        for args in ((True, False), (-1, True), (1, 'yes'), (1, True, 'bad'),
                     (1, False, []), (1, True, 'display-rotation-lock-failed')):
            with self.subTest(args=args), self.assertRaises(ValueError):
                client.session_update(*args)
        self.assertFalse(connection.calls)

    def test_validated_service_denial_is_classified_for_the_shared_runtime(self):
        client, connection = self.client()
        client.get_state()
        connection.error = HardwareOperationError('access-denied', validated_hardware_state(state()))
        with self.assertRaises(ServiceError) as caught:
            client.session_update(1, True)
        self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        self.assertTrue(connection.closed)

    def test_two_workers_get_independent_channels_and_reject_cross_instance_feedback(self):
        made = []
        def factory():
            connection = Connection(state('one' if not made else 'two'))
            made.append(connection)
            return connection
        client = WindowsSessionClient(factory=factory)
        self.addCleanup(client.close)
        client.get_state()
        with ThreadPoolExecutor(max_workers=1) as executor:
            result = executor.submit(client.session_update, 1, True)
            with self.assertRaises(ServiceError) as caught:
                result.result(timeout=3)
        self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        self.assertEqual(made[1].calls, [('get_state', {})])
        self.assertTrue(made[1].closed)
        self.assertEqual(client.get_state().instance, 'two')
        self.assertTrue(made[0].closed)

    def test_lease_worker_does_not_wait_for_a_blocked_state_channel(self):
        entered, release = threading.Event(), threading.Event()
        made = []
        class Blocked(Connection):
            def request(self, method, params):
                if len(self.calls) == 1:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('fixture state request did not release')
                return super().request(method, params)
        def factory():
            connection = Blocked() if not made else Connection()
            made.append(connection)
            return connection
        client = WindowsSessionClient(factory=factory)
        self.addCleanup(client.close)
        with ThreadPoolExecutor(max_workers=1) as state_worker, ThreadPoolExecutor(max_workers=1) as lease_worker:
            self.assertEqual(state_worker.submit(client.get_state).result(timeout=3).instance, 'native-one')
            blocked = state_worker.submit(client.get_state)
            self.assertTrue(entered.wait(3))
            try:
                lease_worker.submit(client.session_update, 1, False).result(timeout=2)
                self.assertEqual(made[1].calls[-1][0], 'session_update')
            finally:
                release.set()
            blocked.result(timeout=3)

    def test_restart_invalidates_an_older_inflight_state_on_the_other_channel(self):
        entered, release = threading.Event(), threading.Event()
        made = []
        class Blocked(Connection):
            def request(self, method, params):
                if len(self.calls) == 1:
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('fixture state request did not release')
                return super().request(method, params)
        def factory():
            connection = Blocked(state('one')) if not made else Connection(state('two'))
            made.append(connection)
            return connection
        client = WindowsSessionClient(factory=factory)
        self.addCleanup(client.close)
        with ThreadPoolExecutor(max_workers=1) as state_worker, ThreadPoolExecutor(max_workers=1) as lease_worker:
            state_worker.submit(client.get_state).result(timeout=3)
            old = state_worker.submit(client.get_state)
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(ServiceError):
                    lease_worker.submit(client.session_update, 1, True).result(timeout=3)
            finally:
                release.set()
            with self.assertRaises(ServiceError) as caught:
                old.result(timeout=3)
            self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        self.assertTrue(all(connection.closed for connection in made))
        self.assertFalse(any(method == 'session_update' for connection in made for method, _ in connection.calls))

    def test_operation_error_from_replacement_service_also_invalidates_the_instance(self):
        made = []
        def factory():
            connection = Connection()
            if made:
                connection.error = HardwareOperationError('config-write-failed', validated_hardware_state(state('two')))
            made.append(connection)
            return connection
        client = WindowsSessionClient(factory=factory)
        self.addCleanup(client.close)
        client.get_state()
        with ThreadPoolExecutor(max_workers=1) as worker:
            with self.assertRaises(ServiceError) as caught:
                worker.submit(client.get_state).result(timeout=3)
        self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        self.assertTrue(made[1].closed)

    def test_close_disallows_new_connections(self):
        client, connection = self.client()
        client.get_state()
        client.close()
        with self.assertRaises(OSError):
            client.get_state()
        self.assertTrue(connection.closed)

    def test_third_worker_cannot_create_an_unbounded_channel(self):
        release = threading.Event()
        client = WindowsSessionClient(factory=Connection)
        self.addCleanup(client.close)
        def hold_worker():
            client.get_state()
            if not release.wait(3):
                raise TimeoutError('fixture worker did not release')
        with ThreadPoolExecutor(max_workers=2) as executor:
            one, two = executor.submit(hold_worker), executor.submit(hold_worker)
            deadline = time.monotonic() + 3
            while len(client._channels) < 2 and time.monotonic() < deadline:
                threading.Event().wait(.01)
            try:
                self.assertEqual(len(client._channels), 2)
                with self.assertRaises(OSError):
                    client.get_state()
            finally:
                release.set()
            one.result(timeout=3)
            two.result(timeout=3)


class ConnectionReceiptTests(unittest.TestCase):
    def connection(self, *, code=None, accepted=None, snapshot=None):
        class Pipes:
            closed = False
            serial = 0
            pose = None
            omit_pose = False
            write_count = 0
            pending_writes = 0
            def connect(self, name):
                return 12
            def write(self, handle, value):
                self.write_count += 1
                self.serial = value['id']
                if self.pending_writes:
                    self.pending_writes -= 1
                    return PipePoll.pending()
                return PipePoll.ready()
            def read(self, handle):
                response = dict(version=1, channel=CHANNEL, type='response', id=self.serial,
                    instance='native-one', accepted=(code is None) if accepted is None else accepted,
                    error=code, state=snapshot or state(), hardware_release_pose=self.pose)
                if self.omit_pose:
                    del response['hardware_release_pose']
                return PipePoll.ready(response)
            def close(self, handle):
                self.closed = True
        pipes = Pipes()
        return HardwareConnection(pipes=pipes), pipes

    def test_operation_failure_keeps_an_authenticated_connection_but_denial_closes_it(self):
        for code, closed in (('config-write-failed', False), ('access-denied', True)):
            connection, pipes = self.connection(code=code)
            pipes.pose = 'tent'
            with self.assertRaises(HardwareOperationError) as caught:
                connection.request('get_state', {})
            self.assertEqual(caught.exception.state.instance, 'native-one')
            self.assertEqual(caught.exception.hardware_release_pose, 'tent')
            self.assertEqual(pipes.closed, closed)
            connection.close()

    def test_release_pose_is_optional_but_must_be_known(self):
        connection, pipes = self.connection()
        pipes.omit_pose = True
        self.assertIsNone(connection.request('get_state', {}).get('hardware_release_pose'))
        connection.close()
        connection, pipes = self.connection()
        pipes.pose = 'tablet'
        with self.assertRaises(ProtocolError):
            connection.request('get_state', {})
        self.assertTrue(pipes.closed)

    def test_client_polls_overlapped_writes_within_the_same_timeout(self):
        connection, pipes = self.connection()
        pipes.pending_writes = 2
        self.assertEqual(connection.request('get_state', {})['accepted'], True)
        self.assertEqual(pipes.write_count, 3)
        connection.close()

    def test_invalid_capabilities_and_inconsistent_results_close_the_channel(self):
        for snapshot, code, accepted in ((dict(state(), sensor_source='iio'), None, True),
                (dict(state(), support_tier='unsupported'), None, True),
                (dict(state(), target=[]), None, True), (state(), 'config-write-failed', True),
                (state(), None, False)):
            connection, pipes = self.connection(code=code, accepted=accepted, snapshot=snapshot)
            with self.assertRaises(ProtocolError):
                connection.request('get_state', {})
            self.assertTrue(pipes.closed)


class NativeSessionRuntimeTests(unittest.TestCase):
    """Real adapters and shared transactions; only OS/driver ports are fakes."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.now = 10.
        self.active = True
        self.receipts = []
        self.peer = ConsoleIdentity('S-1-5-21-1000', 100, 1, 42, 100000)
        self.input = SimulatedInput()
        # The synthetic source uses the native contract to exercise its codec;
        # no device is queried and this is not native sensor/input evidence.
        self.engine = Engine(controller=Controller(), presentation=PosePresentation(),
            sensor=SimpleNamespace(info=SensorInfo(ready=True, backend='windows-winrt'),
                reading=lambda: SensorReading((0., 0., -9.81), self.now, 'windows-winrt')),
            actuator=self.input, settings=None, device=DeviceInfo(platform='windows-fixture'),
            hardware_query=lambda: (False, HardwareStatus.OK), monotonic=lambda: self.now,
            executor=InlineExecutor())
        self.addCleanup(self.engine.shutdown)
        for _ in range(80):
            self.now += .05
            self.engine.tick()
        self.host = HardwareHost(self.engine, authorize=lambda peer: self.active, session_process=self.peer)
        self.journal = Journal(Path(directory.name) / 'display.json')
        fixture = self
        class Api:
            current = Panel('fixture-panel', r'\\.\DISPLAY1', (1, 0, 0), 11, 0, 2880, 1920, 1)
            fail = False
            def read(self):
                return self.current
            def lock(self, locked):
                self.current = replace(self.current, auto_rotation=1 if locked else 0)
            def rotate(self, identity, previous, transform):
                self.current = replace(self.current, transform=transform)
                if self.fail:
                    self.fail = False
                    from panasonic_posture.session.failures import DisplayError
                    raise DisplayError('display-transform-not-applied')
        self.api = Api()
        self.display = WindowsDisplay(api=self.api, journal=self.journal)
        class Pipes:
            reply = None
            def connect(self, name):
                return 12
            def write(self, handle, request):
                if request['method'] == 'session_update':
                    fixture.receipts.append((request['params'], fixture.api.current.transform))
                try:
                    self.reply = fixture.host.dispatch(fixture.peer, request)
                    self.reply['error'] = None
                except EngineError as error:
                    self.reply = dict(version=1, channel=CHANNEL, type='response', id=request['id'],
                        instance=fixture.host.instance, accepted=False, error=error.code,
                        state=fixture.engine.state.to_dict())
                return PipePoll.ready()
            def read(self, handle):
                reply, self.reply = self.reply, None
                return PipePoll.ready(reply) if reply is not None else PipePoll.idle()
            def close(self, handle):
                fixture.host.disconnected(fixture.peer)
        self.client = WindowsSessionClient(factory=lambda: HardwareConnection(pipes=Pipes()))
        self.addCleanup(self.client.close)
        self.runtime = SessionRuntime(self.client, self.display, None,
            executor=InlineExecutor(), lease_executor=InlineExecutor())
        self.addCleanup(self.runtime.close)

    def step(self):
        self.now += .05
        self.host.tick()
        self.runtime.tick(self.now)
        self.engine.tick()

    def tent(self):
        self.engine.set_mode('tent')
        self.engine.tick()
        self.assertIsNone(self.engine.state.applied)
        self.assertFalse(self.input.running)
        self.step()

    def test_matching_wire_feedback_follows_display_commit_and_shutdown_restores(self):
        self.tent()
        self.assertEqual(self.engine.state.applied.mode, 'tent')
        self.assertTrue(self.input.running)
        ready = [(params, rotation) for params, rotation in self.receipts if params['ready']]
        self.assertTrue(ready)
        self.assertTrue(all(params['generation'] == self.engine.state.generation and rotation == 2
                            for params, rotation in ready))
        self.assertEqual(self.journal.load()['written_transform'], 2)
        self.runtime.close()
        self.client.close()
        self.assertEqual(self.api.current.transform, 0)
        self.assertIsNone(self.journal.load())
        self.assertFalse(self.input.running)
        self.assertIsNone(self.engine.state.applied)

    def test_failed_display_never_acknowledges_or_acquires_input(self):
        self.api.fail = True
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.tent()
        self.assertIsNone(self.engine.state.applied)
        self.assertFalse(self.input.running)
        self.assertEqual(self.api.current.transform, 0)
        self.assertTrue(self.receipts)
        self.assertTrue(all(not params['ready'] for params, _ in self.receipts))

    def test_console_rejection_releases_input_and_restores_owned_rotation(self):
        self.tent()
        self.active = False
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.step()
        self.assertTrue(self.runtime.failure_active)
        self.assertFalse(self.input.running)
        self.assertIsNone(self.engine.state.applied)
        self.assertEqual(self.api.current.transform, 0)
        self.assertIsNone(self.journal.load())

    def test_agent_reuses_workers_and_separates_screen_off_from_lost_qualification(self):
        lock = threading.RLock()
        dispatch = self.host.dispatch
        def serial_dispatch(peer, request):
            with lock:
                return dispatch(peer, request)
        self.host.dispatch = serial_dispatch
        context = SimpleNamespace(absent=False, screen_off=False, poll=lambda: None, close=lambda: None)
        context.unattended = lambda: context.absent or context.screen_off
        context.control_available = lambda: not context.absent
        self.client.unavailable = lambda: not context.control_available()
        self.engine.set_mode('tent')
        self.engine.tick()
        agent = WindowsSessionAgent(context_factory=lambda: context,
            client_factory=lambda unattended: self.client, display_factory=lambda client: self.display)
        self.addCleanup(agent.close)
        agent.start()
        def until(predicate):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with lock:
                    self.now += .05
                    self.host.tick()
                agent.tick(self.now)
                with lock:
                    if predicate():
                        return
                time.sleep(.005)
            self.fail('native session assembly did not reach expected state')
        until(lambda: self.engine.state.applied is not None)
        # A display operation that spans the heartbeat window may already
        # have created the independent lease lane. It must remain bounded.
        self.assertLessEqual(len(self.client._channels), 2)
        context.screen_off = True
        beginning = self.now
        until(lambda: self.now - beginning > .6)
        self.assertEqual(self.engine.state.applied.mode, 'tent')
        self.assertEqual(self.api.current.transform, 2)
        self.assertTrue(self.input.running)
        self.assertIsNone(agent._qualification_task)
        context.absent = True
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            until(lambda: agent.runtime.failure_active and self.api.current.transform == 0)
        self.assertFalse(self.input.running)
        self.assertIsNone(self.engine.state.applied)
        self.assertEqual(len(self.client._channels), 2)
        # Move beyond a lease period while qualification is unavailable. The independent lane
        # keeps the same authenticated context, without reporting ready.
        beginning = self.now
        until(lambda: self.now - beginning > 4)
        self.assertEqual(self.engine.session_owner, self.peer.owner)
        self.assertLess(self.now - self.engine.agent_seen, 3)
        self.assertFalse(self.input.running)
        context.absent = False
        until(lambda: self.engine.state.applied is not None)
        self.assertEqual(self.engine.state.applied.mode, 'tent')
        self.assertEqual(self.api.current.transform, 2)
        self.assertEqual(len(self.client._channels), 2)
        agent.close()
        self.assertFalse(self.input.running)
        self.assertIsNone(self.journal.load())
