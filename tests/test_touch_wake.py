"""Touch wake uses fake devices, clocks and buses; it never touches the host."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from gi.repository import GLib

import test_agent as session_fixtures
import test_engine as engine_fixtures
import test_access as access_fixtures
from panasonic_posture.model import StateSnapshot, dumps_state, parse_state_snapshot
from panasonic_posture.service.actuator import Actuator
from panasonic_posture.service.ports import SessionOwner, TouchActivity
from panasonic_posture.session.runtime import SessionRuntime
from panasonic_posture.session.client import SystemClient
from panasonic_posture.session.failures import FailureKind, ServiceError
from panasonic_posture.tasks import InlineExecutor


class TouchWireTests(unittest.TestCase):
    def test_optional_wake_token_round_trip_and_legacy_default(self):
        state = StateSnapshot(touch_wake=9)
        self.assertEqual(parse_state_snapshot(dumps_state(state)).touch_wake, 9)
        document = state.to_dict()
        document.pop("touch_wake")
        self.assertIsNone(parse_state_snapshot(json.dumps(document)).touch_wake)

    def test_invalid_wake_tokens_are_rejected(self):
        for value in (True, 0, -1, 1.5, "1", 2**64):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_state_snapshot(json.dumps(dict(StateSnapshot().to_dict(), touch_wake=value)))

    def test_client_sends_generation_and_uint64_touch_token(self):
        client = SystemClient(object())
        with mock.patch.object(client, "_call") as call:
            client.wake_screen(7, 9)
        self.assertEqual(call.call_args.args[0], "SessionWake")
        self.assertEqual(call.call_args.args[1].get_type_string(), "(ut)")
        self.assertEqual(call.call_args.args[1].unpack(), (7, 9))


class TouchActuatorTests(unittest.TestCase):
    def make_actuator(self):
        self.clock = session_fixtures.Ticker(10.0)
        actuator = Actuator(monotonic=self.clock)
        actuator._request = 1
        actuator.process = SimpleNamespace(stdin=io.BytesIO(), poll=lambda: None)
        self.addCleanup(lambda: setattr(actuator, "process", None))
        return actuator

    def report(self, actuator, serial=1, request=1):
        actuator._accept_report(json.dumps(dict(
            protocol=4, kind="applied", request=request, tablet=True,
            input_disabled=True, hardware=False, physical=False,
            touch_serial=serial,
        )).encode(), self.clock())

    def test_touch_receipt_is_fresh_consumed_once_and_does_not_renew_lease(self):
        actuator = self.make_actuator()
        self.report(actuator)
        event = actuator.touch_activity
        self.assertIsNotNone(event)
        actuator.request_wake(event.serial)
        actuator._heartbeat(self.clock())
        sent = actuator.process.stdin.getvalue()
        self.assertIn(b"w\n", sent)
        actuator.request_wake(event.serial)
        actuator._heartbeat(self.clock())
        self.assertEqual(actuator.process.stdin.getvalue(), sent)
        self.report(actuator)
        self.assertEqual(actuator.touch_activity, event)

    def test_stale_command_invalid_serial_and_old_touch_cannot_wake(self):
        for changes in (dict(request=0), dict(serial=True), dict(serial=-1)):
            actuator = self.make_actuator()
            self.report(actuator, **changes)
            self.assertIsNone(actuator.touch_activity)
        actuator = self.make_actuator()
        self.report(actuator)
        self.clock.advance(3)
        actuator.request_wake(actuator.touch_activity.serial)
        actuator._heartbeat(self.clock())
        self.assertNotIn(b"w", actuator.process.stdin.getvalue())

    def test_out_of_order_receipts_do_not_refresh_contact(self):
        actuator = self.make_actuator()
        self.report(actuator, serial=2)
        event = actuator.touch_activity
        self.clock.advance(1)
        self.report(actuator, serial=1)
        self.assertEqual(actuator.touch_activity, event)


class TouchEngineTests(engine_fixtures.EngineTestCase):
    def prepare(self):
        engine_fixtures.EngineTestCase.setUp(self)
        engine = self.make()
        self.lease(engine)
        self.controller.target = engine_fixtures.TENT
        engine._target = engine_fixtures.TENT
        self.controller.applied = engine_fixtures.TENT
        engine.agent_ready = self.controller.generation
        self.actuator.touch_activity = TouchActivity(9, self.mono())
        self.actuator.request_wake = mock.Mock()
        return engine

    def test_only_current_owner_ready_generation_and_recent_touch_can_request_wake(self):
        engine = self.prepare()
        owner = engine.session_owner
        generation = self.controller.generation
        engine.session_wake(generation, 9, owner)
        self.actuator.request_wake.assert_not_called()
        engine._flush_touch_wake()
        self.actuator.request_wake.assert_called_once_with(9)

    def test_invalid_requests_never_reach_the_actuator(self):
        for case in ("owner", "generation", "expired", "token", "unready", "unapplied", "error", "sleep"):
            with self.subTest(case=case):
                engine = self.prepare()
                owner, generation, token = engine.session_owner, self.controller.generation, 9
                if case == "owner":
                    owner = SessionOwner("linux-uid", "2000")
                elif case == "generation":
                    generation += 1
                elif case == "expired":
                    self.actuator.touch_activity = TouchActivity(9, self.mono() - 3)
                elif case == "token":
                    token = 10
                elif case == "unready":
                    engine.agent_ready = None
                elif case == "unapplied":
                    self.controller.applied = None
                elif case == "error":
                    self.controller.error_code = "internal-error"
                elif case == "sleep":
                    engine.suspended = True
                engine.session_wake(generation, token, owner)
                engine._flush_touch_wake()
                self.actuator.request_wake.assert_not_called()

    def test_queued_request_is_rechecked_after_mode_or_owner_changes(self):
        for case in ("generation", "owner", "expired"):
            with self.subTest(case=case):
                engine = self.prepare()
                engine.session_wake(self.controller.generation, 9, engine.session_owner)
                self.assertIsNotNone(engine._pending_wake)
                if case == "generation":
                    self.controller.generation += 1
                elif case == "owner":
                    engine.session_owner = SessionOwner("linux-uid", "2000")
                else:
                    self.mono.value += 3
                engine._flush_touch_wake()
                self.actuator.request_wake.assert_not_called()
                self.assertIsNone(engine._pending_wake)

    def test_session_to_service_round_trip_preserves_applied_posture(self):
        engine = self.prepare()
        owner = engine.session_owner
        generation = self.controller.generation
        client = SimpleNamespace(
            get_state=lambda: engine._build_state(engine._lease_active(self.mono())),
            session_update=lambda *args: engine.session_update(*args, owner=owner),
            wake_screen=lambda gen, token: engine.session_wake(gen, token, owner),
        )
        display = session_fixtures.FakeDisplay()
        runtime = SessionRuntime(client, display, None, executor=InlineExecutor())
        runtime.tick(self.mono())
        self.actuator.touch_activity = TouchActivity(10, self.mono())
        runtime.locked = True
        runtime.tick(self.mono())
        engine._flush_touch_wake()
        self.actuator.request_wake.assert_called_once_with(10)
        self.assertEqual(engine._target, engine_fixtures.TENT)
        self.assertEqual(self.controller.applied, engine_fixtures.TENT)
        self.assertEqual(self.controller.generation, generation)
        self.assertTrue(runtime.ready)
        self.assertEqual(display.restores, 0)


class TouchSessionTests(unittest.TestCase):
    def prepare(self):
        self.state = session_fixtures.make_state()
        self.state.applied = self.state.target
        self.client = session_fixtures.FakeClient(self.state)
        self.client.wake_screen = mock.Mock()
        self.display = session_fixtures.FakeDisplay()
        self.runtime = SessionRuntime(self.client, self.display, None, executor=InlineExecutor())
        self.runtime.tick(0)

    def test_touch_wakes_locked_session_without_changing_posture_or_readiness(self):
        self.prepare()
        self.runtime.locked = True
        self.state.touch_wake = 8
        self.runtime.tick(0.1)
        self.runtime.tick(0.2)
        self.client.wake_screen.assert_called_once_with(1, 8)
        self.assertTrue(self.runtime.ready)
        self.assertEqual(len(self.display.applied), 1)
        self.assertEqual(self.display.restores, 0)

    def test_no_replay_after_restart_failure_or_unapplied_state(self):
        for case in ("restart", "error", "disconnected", "unapplied"):
            with self.subTest(case=case):
                self.prepare()
                self.state.touch_wake = 8
                if case == "restart":
                    self.state.instance = "new"
                elif case == "error":
                    self.state.error_code = "relay-exited"
                elif case == "disconnected":
                    self.state.session_connected = False
                else:
                    self.state.applied = None
                self.runtime.tick(0.1)
                self.client.wake_screen.assert_not_called()

    def test_slow_wake_does_not_delay_heartbeats_or_display_work(self):
        self.prepare()
        wake_executor = session_fixtures.DeferredExecutor()
        self.runtime._wake_executor = wake_executor
        self.state.touch_wake = 8
        self.runtime.tick(0.1)
        self.assertEqual(len(wake_executor.calls), 1)
        self.runtime.tick(1)
        self.assertTrue(self.client.updates[-1][1])
        self.assertEqual(self.runtime.last_feedback_at, 1)
        self.state.generation = 2
        self.runtime.tick(1.1)
        self.assertEqual(len(self.display.applied), 2)
        wake_executor.run_next()
        self.runtime.tick(1.2)
        self.assertTrue(self.runtime.ready)

    def test_wake_identity_rejection_uses_existing_recovery(self):
        self.prepare()
        self.client.wake_screen.side_effect = ServiceError(
            "access-denied", "inactive session", kind=FailureKind.REJECTED)
        self.state.touch_wake = 8
        self.runtime.tick(0.1)
        self.assertTrue(self.runtime.failure_active)
        self.assertFalse(self.runtime.ready)
        self.assertEqual(self.display.restores, 1)

    def test_transient_wake_failure_keeps_applied_posture(self):
        self.prepare()
        self.client.wake_screen.side_effect = OSError("disconnected")
        self.state.touch_wake = 8
        self.runtime.tick(0.1)
        self.assertTrue(self.runtime.ready)
        self.assertFalse(self.runtime.failure_active)
        self.assertEqual(self.display.restores, 0)


class TouchAccessTests(unittest.TestCase):
    Invocation = access_fixtures.DaemonAuthBoundaryTests.Invocation
    make_daemon = access_fixtures.DaemonAuthBoundaryTests.make_daemon
    call = access_fixtures.DaemonAuthBoundaryTests.call
    def test_wake_uses_session_identity_instead_of_ui_authorization(self):
        daemon = self.make_daemon()
        invocation = self.call(daemon, "SessionWake", GLib.Variant("(ut)", (7, 9)))
        self.assertIsNone(invocation.error)
        daemon.application.access.verify_session_adapter.assert_called_once()
        daemon.application.access.authorize.assert_not_called()
        daemon.application.engine.session_wake.assert_called_once_with(
            7, 9, SessionOwner("linux-uid", "1000"))

    def test_other_session_processes_cannot_request_wake(self):
        daemon = self.make_daemon(verify_error=access_fixtures.AccessDenied("not adapter"))
        invocation = self.call(daemon, "SessionWake", GLib.Variant("(ut)", (7, 9)))
        self.assertIsNotNone(invocation.error)
        daemon.application.engine.session_wake.assert_not_called()

    def test_slow_wake_authorization_does_not_occupy_the_heartbeat_queue(self):
        daemon = self.make_daemon()
        worker = mock.Mock()
        worker.submit.return_value = session_fixtures.Future()
        daemon._authorization_workers["wake"] = worker
        pending = self.call(daemon, "SessionWake", GLib.Variant("(ut)", (7, 9)))
        self.assertIsNone(pending.value)
        heartbeat = self.call(daemon, "SessionUpdate", GLib.Variant("(ubss)", (7, True, "", "")))
        self.assertIsNotNone(heartbeat.value)
        daemon.application.engine.session_update.assert_called_once_with(
            7, True, "", "", SessionOwner("linux-uid", "1000"))
