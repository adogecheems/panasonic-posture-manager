"""Tests for the session adapter state machine and the D-Bus client.

The adapter is driven with fake D-Bus/display/notification objects and a fake
clock; no real GNOME session, hardware or system bus is involved.
"""

import copy
import unittest
from concurrent.futures import Executor, Future
from unittest import mock

from gi.repository import Gio, GLib

from panasonic_posture import protocol
from panasonic_posture.model import StateSnapshot, Target
from panasonic_posture.protocol import ErrorCode
from panasonic_posture.session.agent import Agent
from panasonic_posture.session.client import SystemClient
from panasonic_posture.session.display import DisplayError
from panasonic_posture.session.failures import ServiceError, FailureKind
from panasonic_posture.session.dbus_errors import service_error
from panasonic_posture.session.runtime import SessionRuntime
from panasonic_posture.tasks import InlineExecutor


def make_state(
    generation=1,
    instance="instance-a",
    pending_notification=None,
    pending_prompt=None,
    error_code=None,
    target=None,
    session_connected=True,
):
    return StateSnapshot(
        instance=instance,
        generation=generation,
        pose="tent",
        manual="auto",
        tent_prompt=True,
        target=target
        or Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True),
        pending_notification=pending_notification,
        pending_prompt=pending_prompt,
        error_code=error_code,
        error_detail=None,
        session_connected=session_connected,
    )


class Ticker:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class DeferredExecutor(Executor):
    """Hold submitted work until the test explicitly releases it."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, /, *args, **kwargs):
        future = Future()
        self.calls.append((future, fn, args, kwargs))
        return future

    def run_next(self):
        future, fn, args, kwargs = self.calls.pop(0)
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)

    def shutdown(self, wait=True, *, cancel_futures=False):
        del wait, cancel_futures


class FakeSystemBus:
    """Minimal system bus: GetNameOwner/GetConnectionUnixUser for the adapter."""

    def __init__(self, owner=None, owner_uid=1000, error=None, uid_error=None):
        self.owner = owner
        self.owner_uid = owner_uid
        self.error = error
        self.uid_error = uid_error
        self.calls = []

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable):
        self.calls.append((method, params))
        if method == "GetNameOwner":
            if self.error is not None:
                raise self.error
            return FakeReply((self.owner,))
        if method == "GetConnectionUnixUser":
            if self.uid_error is not None:
                raise self.uid_error
            return FakeReply((self.owner_uid,))
        raise AssertionError(f"unexpected bus call {method}")


class FakeLoop:
    def __init__(self, events):
        self.events = events

    def run(self):
        self.events.append("run")

    def quit(self):
        self.events.append("quit")


class FakeClient:
    def __init__(self, state, events=None):
        self.state = state
        self.events = events
        self.updates = []
        self.update_error = None
        self.get_error = None
        self.confirmations = []
        self.stand_confirmations = []
        self.get_calls = 0

    def get_state(self):
        self.get_calls += 1
        if self.events is not None:
            self.events.append("get_state")
        if self.get_error is not None:
            raise self.get_error
        return copy.deepcopy(self.state)

    def session_update(self, generation, ready, error_code, error_detail):
        self.updates.append((generation, ready, error_code, error_detail))
        if self.update_error is not None:
            raise self.update_error

    def confirm_tent(self, epoch):
        self.confirmations.append(epoch)

    def confirm_stand(self, epoch):
        self.stand_confirmations.append(epoch)


class FakeDisplay:
    def __init__(self, errors=()):
        self.errors = list(errors)
        self.attempts = []
        self.applied = []
        self.restores = 0
        self.apply_hook = None

    def apply(self, target):
        self.attempts.append(target)
        if self.errors:
            error = self.errors.pop(0)
            if error is not None:
                raise error
        self.applied.append(target)
        if self.apply_hook is not None:
            self.apply_hook(target)

    def restore(self, normal=False):
        self.restores += 1


class FakeNotifier:
    def __init__(self):
        self.sync_calls = []
        self.close_calls = 0
        self.notification = 0
        self.epoch = None
        self.shown_epoch = None
        self.prompt = None
        self.last_prompt = None

    def sync(self, epoch, prompt="tent", allow_retry=True, now=None):
        self.sync_calls.append((epoch, allow_retry))
        self.last_prompt = prompt

    def close(self):
        self.close_calls += 1
        self.notification = 0
        self.epoch = None
        self.shown_epoch = None
        self.prompt = None


class Harness:
    def __init__(self, state, *, locked=False, display=None, executor=None):
        self.clock = Ticker()
        self.events = []
        self.lock_state = {"locked": locked}
        self.client = FakeClient(state, self.events)
        self.display = display or FakeDisplay()
        self.notifier = FakeNotifier()
        self.executor = executor if executor is not None else InlineExecutor()
        self.agent = Agent(
            uid=1000,
            clock=self.clock,
            screen_locked=lambda: self.lock_state["locked"],
            install_signals=False,
            executor=self.executor,
        )
        self.agent.attach(self.client, self.display, self.notifier)

    def tick(self, now=None):
        self.agent.tick(self.clock.now if now is None else now)

    def set_state(self, state):
        self.client.state = state


class AdapterTickTests(unittest.TestCase):
    def test_generation_change_applies_target_and_reports_ready(self):
        harness = Harness(make_state(pending_notification=7))
        harness.tick(0.0)
        self.assertEqual(len(harness.display.applied), 1)
        target = harness.display.applied[0]
        self.assertEqual(target.mode, "tent")
        self.assertTrue(target.tablet)
        self.assertEqual(target.rotation, 180)
        self.assertEqual(harness.client.updates[-1], (1, True, "", ""))
        self.assertEqual(harness.notifier.sync_calls[-1][0], 7)
        self.assertEqual(harness.notifier.last_prompt, "tent")
        self.assertTrue(harness.agent.ready)

    def test_generation_change_during_apply_is_verified_before_ready(self):
        harness = Harness(make_state())

        def switch_generation(_target):
            harness.set_state(
                make_state(
                    generation=2,
                    target=Target(
                        mode="laptop",
                        tablet=False,
                        rotation=0,
                        disable_internal_input=False,
                    ),
                )
            )

        harness.display.apply_hook = switch_generation
        harness.tick(0.0)

        self.assertEqual(
            [target.mode for target in harness.display.applied], ["tent", "laptop"]
        )
        self.assertEqual(harness.agent.runtime.generation, 2)
        self.assertTrue(harness.agent.ready)
        self.assertEqual(harness.client.updates, [(2, True, "", "")])

    def test_tick_never_runs_blocking_work_inline(self):
        executor = DeferredExecutor()
        harness = Harness(make_state(), executor=executor)

        harness.tick(0.0)
        self.assertEqual(harness.client.get_calls, 0)
        self.assertEqual(harness.display.attempts, [])
        self.assertEqual(harness.client.updates, [])

        executor.run_next()  # GetState
        harness.tick(0.1)
        self.assertEqual(harness.display.attempts, [])
        self.assertEqual(harness.client.updates, [])

        executor.run_next()  # display.apply
        harness.tick(0.2)
        self.assertEqual(harness.display.attempts, [make_state().target])
        self.assertEqual(harness.client.updates, [])

        executor.run_next()  # post-apply GetState verification
        harness.tick(0.3)
        executor.run_next()  # final SessionUpdate
        harness.tick(0.4)
        self.assertTrue(harness.agent.ready)
        self.assertEqual(harness.client.updates, [(1, True, "", "")])

    def test_worker_completions_advance_without_waiting_for_timer(self):
        executor = DeferredExecutor()
        harness = Harness(make_state(), executor=executor)
        callbacks = []

        with mock.patch.object(GLib, "idle_add", side_effect=callbacks.append):
            harness.tick()
            for _ in range(2):  # GetState and the display transaction
                executor.run_next()
                self.assertEqual(len(callbacks), 1)
                self.assertEqual(executor.calls, [])
                self.assertFalse(callbacks.pop(0)())

            # The display was applied, but the target changed before readback.
            self.assertEqual(harness.client.updates, [])
            laptop = Target(mode="laptop", tablet=False, rotation=0)
            harness.set_state(make_state(generation=2, target=laptop))
            for _ in range(4):  # verify, apply, verify, feedback
                executor.run_next()
                self.assertEqual(len(callbacks), 1)
                self.assertFalse(callbacks.pop(0)())

        self.assertEqual(harness.display.applied, [make_state().target, laptop])
        self.assertEqual(harness.client.get_calls, 3)
        self.assertEqual(harness.client.updates, [(2, True, "", "")])
        self.assertEqual(executor.calls, [])
        self.assertEqual(callbacks, [])

    def test_stand_prompt_kind_is_forwarded_to_notifier(self):
        harness = Harness(
            make_state(pending_notification=8, pending_prompt="stand")
        )
        harness.tick(0.0)
        self.assertEqual(harness.notifier.sync_calls[-1][0], 8)
        self.assertEqual(harness.notifier.last_prompt, "stand")

    def test_heartbeat_is_throttled(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        sent = len(harness.client.updates)
        harness.tick(0.1)
        self.assertEqual(len(harness.client.updates), sent)
        harness.tick(protocol.AGENT_HEARTBEAT_SECONDS + 0.1)
        self.assertGreater(len(harness.client.updates), sent)

    def test_display_failure_reports_code_and_retries_with_backoff(self):
        display = FakeDisplay(
            errors=[DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch")]
        )
        harness = Harness(make_state(), display=display)
        harness.tick(0.0)
        self.assertEqual(
            harness.client.updates[-1],
            (1, False, ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch"),
        )
        harness.tick(0.5)
        self.assertEqual(len(display.attempts), 1)  # still backing off
        harness.tick(protocol.DISPLAY_RETRY_BASE_SECONDS + 0.1)
        self.assertEqual(len(display.attempts), 2)
        self.assertEqual(len(display.applied), 1)
        self.assertEqual(harness.client.updates[-1], (1, True, "", ""))
        self.assertTrue(harness.agent.ready)

    def test_lease_failure_stops_transactions_and_recovers(self):
        harness = Harness(make_state())
        harness.client.update_error = service_error(
            ErrorCode.INTERNAL, "denied", name="org.freedesktop.DBus.Error.AccessDenied"
        )
        harness.tick(0.0)
        self.assertEqual(len(harness.display.applied), 1)
        self.assertEqual(harness.display.restores, 1)
        self.assertGreaterEqual(harness.notifier.close_calls, 1)
        self.assertTrue(harness.agent._failure_active)
        calls_after_failure = harness.client.get_calls
        # Inside the backoff window no display work and no service calls happen.
        harness.tick(1.0)
        self.assertEqual(harness.client.get_calls, calls_after_failure)
        self.assertEqual(len(harness.display.applied), 1)
        # Recover: the probe succeeds, the state is re-read and the target reapplied.
        harness.client.update_error = None
        harness.tick(protocol.DISPLAY_RETRY_BASE_SECONDS + 0.1)
        self.assertFalse(harness.agent._failure_active)
        self.assertEqual(len(harness.display.applied), 2)
        self.assertTrue(harness.agent.ready)
        self.assertEqual(harness.client.updates[-1], (1, True, "", ""))

    def test_lease_recovery_probes_current_generation_before_applying(self):
        harness = Harness(make_state())
        harness.client.update_error = service_error(ErrorCode.ACCESS_DENIED, "session-lease-busy")
        harness.tick(0.0)
        harness.set_state(make_state(generation=2))

        def update(generation, ready, code, detail):
            # The service returns success for stale reports without checking lease ownership.
            if generation == harness.client.state.generation:
                raise harness.client.update_error

        harness.client.session_update = update
        harness.tick(2.0)

        self.assertTrue(harness.agent._failure_active)
        self.assertEqual(len(harness.display.applied), 1)

    def test_lease_recovery_does_not_apply_after_probe_becomes_stale(self):
        harness = Harness(make_state())
        harness.client.update_error = service_error(ErrorCode.ACCESS_DENIED, "session-lease-busy")
        harness.tick(0.0)

        for state in (make_state(generation=2), make_state(instance="instance-b")):
            with self.subTest(state=state):
                def update(generation, ready, code, detail):
                    if ready:
                        raise harness.client.update_error
                    harness.set_state(state)

                harness.client.session_update = update
                harness.tick(harness.agent._failure_retry_at)
                self.assertTrue(harness.agent._failure_active)
                self.assertEqual(len(harness.display.applied), 1)

    def test_access_denied_is_an_immediate_lease_failure(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        self.assertTrue(harness.agent.ready)
        harness.client.get_error = service_error(
            ErrorCode.ACCESS_DENIED, "inactive-session", name=protocol.DBUS_ERROR)
        harness.tick(0.6)
        self.assertTrue(harness.agent._failure_active)
        self.assertFalse(harness.agent.ready)
        self.assertEqual(harness.display.restores, 1)

    def test_identity_rejection_detail_is_an_immediate_lease_failure(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        harness.client.update_error = service_error(
            ErrorCode.INTERNAL, "not-session-adapter")
        harness.tick(0.6)
        self.assertTrue(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 1)

    def test_single_transient_get_state_error_does_not_restore_display(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        self.assertTrue(harness.agent.ready)
        calls = harness.client.get_calls
        harness.client.get_error = service_error(
            ErrorCode.INTERNAL, "timed out",
            name="org.freedesktop.DBus.Error.NoReply")
        harness.tick(0.1)
        self.assertFalse(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 0)
        self.assertTrue(harness.agent.ready)
        # The transient retry is throttled instead of hammering the service.
        harness.tick(0.2)
        self.assertEqual(harness.client.get_calls, calls + 1)
        # Recovery within the grace keeps the display untouched.
        harness.client.get_error = None
        harness.tick(0.7)
        self.assertFalse(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 0)

    def test_continuous_transient_failure_restores_after_grace(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        harness.client.get_error = service_error(
            ErrorCode.INTERNAL, "timed out",
            name="org.freedesktop.DBus.Error.NoReply")
        for moment in (0.1, 0.7, 1.4, 2.2, 3.0):
            harness.tick(moment)
        self.assertFalse(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 0)
        harness.tick(3.2)  # > AGENT_FAILURE_GRACE_SECONDS of continuous failure
        self.assertTrue(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 1)

    def test_single_transient_session_update_error_does_not_restore(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        harness.client.update_error = service_error(
            ErrorCode.INTERNAL, "timed out",
            name="org.freedesktop.DBus.Error.NoReply")
        harness.tick(0.6)  # heartbeat fails transiently
        self.assertFalse(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 0)
        self.assertTrue(harness.agent.ready)
        harness.client.update_error = None
        harness.tick(1.2)
        self.assertFalse(harness.agent._failure_active)
        self.assertTrue(harness.agent.ready)

    def test_continuous_session_update_failure_escalates_after_grace(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        harness.client.update_error = service_error(
            ErrorCode.INTERNAL, "timed out",
            name="org.freedesktop.DBus.Error.NoReply")
        for moment in (0.6, 1.2, 1.9, 2.7, 3.5):
            harness.tick(moment)
        self.assertFalse(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 0)
        harness.tick(4.2)  # > grace of continuous heartbeat failures
        self.assertTrue(harness.agent._failure_active)
        self.assertEqual(harness.display.restores, 1)

    def test_lease_failure_backoff_doubles_and_is_capped(self):
        harness = Harness(make_state())
        harness.client.update_error = service_error(ErrorCode.ACCESS_DENIED, "inactive")
        harness.tick(0.0)
        self.assertEqual(harness.agent._failure_retry_at, 2.0)
        harness.tick(2.0)
        self.assertEqual(harness.agent._failure_retry_at, 6.0)
        harness.tick(6.0)
        self.assertEqual(harness.agent._failure_retry_at, 14.0)
        harness.tick(14.0)
        self.assertEqual(
            harness.agent._failure_retry_at,
            14.0 + protocol.DISPLAY_RETRY_MAX_SECONDS,
        )
        for _ in range(1025):
            retry_at = harness.agent._failure_retry_at
            harness.tick(retry_at)
            self.assertEqual(
                harness.agent._failure_retry_at,
                retry_at + protocol.DISPLAY_RETRY_MAX_SECONDS,
            )
        harness.client.update_error = None
        harness.tick(harness.agent._failure_retry_at)
        self.assertTrue(harness.agent.ready)

    def test_tick_guard_keeps_the_glib_source_alive(self):
        harness = Harness(make_state())
        runtime = harness.agent.runtime
        original_tick = runtime.tick
        runtime.tick = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("panasonic_posture.session.agent", level="ERROR"):
            self.assertIs(harness.agent._tick(), GLib.SOURCE_CONTINUE)
        # The next tick works again.
        runtime.tick = original_tick
        self.assertIs(harness.agent._tick(), GLib.SOURCE_CONTINUE)
        self.assertEqual(len(harness.display.applied), 1)

    def test_echoed_agent_error_does_not_block_display_retry(self):
        display = FakeDisplay(
            errors=[DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch")]
        )
        harness = Harness(make_state(), display=display)
        harness.tick(0.0)
        self.assertEqual(harness.client.updates[-1][2], ErrorCode.DISPLAY_TRANSFORM)
        # The daemon may echo the last SessionUpdate error in the state document.
        harness.set_state(make_state(error_code=ErrorCode.DISPLAY_TRANSFORM))
        harness.tick(protocol.DISPLAY_RETRY_BASE_SECONDS + 0.1)
        self.assertEqual(len(display.applied), 1)
        self.assertTrue(harness.agent.ready)
        self.assertEqual(harness.client.updates[-1], (1, True, "", ""))

    def test_stale_echo_after_generation_bump_does_not_block_apply(self):
        display = FakeDisplay(
            errors=[DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch")]
        )
        harness = Harness(make_state(), display=display)
        harness.tick(0.0)
        # The daemon bumped the generation but still carries the old report.
        harness.set_state(make_state(generation=2, error_code=ErrorCode.DISPLAY_TRANSFORM))
        harness.tick(protocol.DISPLAY_RETRY_BASE_SECONDS + 0.1)
        self.assertEqual(len(display.applied), 1)
        self.assertTrue(harness.agent.ready)

    def test_genuine_backend_error_is_not_treated_as_echo(self):
        display = FakeDisplay(
            errors=[DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch")]
        )
        harness = Harness(make_state(), display=display)
        harness.tick(0.0)
        # A different code comes from the privileged backend, not from us.
        harness.set_state(make_state(error_code=ErrorCode.RELAY_EXITED))
        harness.tick(protocol.DISPLAY_RETRY_BASE_SECONDS + 0.1)
        self.assertEqual(len(display.applied), 0)
        self.assertFalse(harness.agent.ready)

    def test_daemon_error_restores_display_and_waits_for_recovery(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        self.assertTrue(harness.agent.ready)
        error_state = make_state(error_code=ErrorCode.RELAY_EXITED)
        harness.set_state(error_state)
        harness.tick(0.6)
        self.assertEqual(harness.display.restores, 1)
        self.assertFalse(harness.agent.ready)
        # While the privileged backend reports an error no display transaction runs.
        harness.tick(1.2)
        self.assertEqual(len(harness.display.attempts), 1)
        # Once the error clears the target is applied again.
        harness.set_state(make_state())
        harness.tick(1.8)
        self.assertEqual(len(harness.display.applied), 2)
        self.assertTrue(harness.agent.ready)

    def test_daemon_error_in_new_generation_restores_previous_display(self):
        harness = Harness(make_state())
        harness.tick(0.0)
        harness.set_state(make_state(
            generation=2,
            target=Target(mode="unavailable"),
            error_code=ErrorCode.RELAY_EXITED,
        ))
        harness.tick(0.1)
        self.assertEqual(harness.display.restores, 1)
        self.assertFalse(harness.agent.ready)
        self.assertEqual(len(harness.display.attempts), 1)

    def test_failed_display_restore_is_retried(self):
        class FlakyRestoreDisplay(FakeDisplay):
            def restore(self, normal=False):
                super().restore(normal)
                if self.restores == 1:
                    raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "compositor busy")

        harness = Harness(make_state(), display=FlakyRestoreDisplay())
        harness.tick(0.0)
        harness.set_state(make_state(error_code=ErrorCode.RELAY_EXITED))
        harness.tick(0.1)
        self.assertEqual(harness.display.restores, 1)
        harness.tick(0.2)
        self.assertEqual(harness.display.restores, 1)
        harness.tick(0.1 + protocol.DISPLAY_RETRY_BASE_SECONDS)
        self.assertEqual(harness.display.restores, 2)
        harness.tick(0.2 + protocol.DISPLAY_RETRY_BASE_SECONDS)
        self.assertEqual(harness.display.restores, 2)

    def test_instance_change_resets_generation_and_notification(self):
        harness = Harness(make_state(instance="a", pending_notification=7))
        harness.tick(0.0)
        self.assertEqual(len(harness.display.applied), 1)
        harness.set_state(make_state(instance="b", pending_notification=None))
        harness.tick(0.2)
        self.assertEqual(len(harness.display.applied), 2)
        self.assertGreaterEqual(harness.notifier.close_calls, 2)

    def test_locked_screen_closes_prompt_and_does_not_show(self):
        harness = Harness(make_state(pending_notification=7), locked=True)
        harness.tick(0.0)
        self.assertTrue(harness.agent.locked)
        self.assertEqual(harness.notifier.sync_calls, [])
        self.assertGreaterEqual(harness.notifier.close_calls, 1)


class AgentLifecycleTests(unittest.TestCase):
    def build_agent(self, events, own_name, system_bus=None):
        harness = Harness(make_state())
        harness.client.events = events

        def build(agent):
            events.append("build")
            agent.attach(harness.client, harness.display, harness.notifier)

        loop = FakeLoop(events)
        agent = Agent(
            system_bus=system_bus if system_bus is not None else FakeSystemBus(),
            session_bus=object(),
            uid=1000,
            own_name=own_name,
            unown_name=lambda token: events.append("unown"),
            loop_factory=lambda: loop,
            timeout_add=lambda _ms, _callback: events.append("timer"),
            install_signals=False,
            screen_locked=lambda: False,
            executor=InlineExecutor(),
            build=build,
        )
        return agent, harness

    def test_bus_name_is_acquired_before_display_and_first_tick(self):
        events = []
        names = []

        def own_name(bus, name, flags, acquired, lost):
            events.append("own")
            names.append(name)
            acquired(bus, name)
            return 7

        agent, harness = self.build_agent(events, own_name)
        code = agent.serve()
        self.assertEqual(code, 0)
        self.assertLess(events.index("own"), events.index("build"))
        self.assertLess(events.index("build"), events.index("timer"))
        self.assertLess(events.index("timer"), events.index("get_state"))
        self.assertLess(events.index("get_state"), events.index("run"))
        self.assertEqual(len(harness.display.applied), 1)
        self.assertIn("unown", events)
        self.assertEqual(names, [protocol.session_name(1000)])

    def test_duplicate_instance_exits_cleanly_without_building(self):
        events = []

        def own_name(bus, name, flags, acquired, lost):
            events.append("own")
            lost(bus, name)
            return 7

        agent, harness = self.build_agent(
            events, own_name,
            system_bus=FakeSystemBus(owner=":1.42", owner_uid=1000))
        self.assertEqual(agent.serve(), 0)
        self.assertNotIn("build", events)
        self.assertNotIn("run", events)
        self.assertIn("unown", events)
        self.assertEqual(harness.display.applied, [])

    def test_policy_denied_name_acquisition_fails_loudly(self):
        events = []
        denied = GLib.Error.new_literal(
            domain=Gio.dbus_error_quark(),
            code=Gio.DBusError.NAME_HAS_NO_OWNER,
            message="GDBus.Error:org.freedesktop.DBus.Error.NameHasNoOwner: no owner",
        )

        def own_name(bus, name, flags, acquired, lost):
            events.append("own")
            lost(bus, name)
            return 7

        agent, _harness = self.build_agent(
            events, own_name, system_bus=FakeSystemBus(error=denied))
        with self.assertLogs("panasonic_posture.session.agent", level="ERROR"):
            self.assertEqual(agent.serve(), 1)

    def test_name_owned_by_another_user_fails_so_systemd_retries(self):
        events = []

        def own_name(bus, name, flags, acquired, lost):
            events.append("own")
            lost(bus, name)
            return 7

        agent, _harness = self.build_agent(
            events, own_name,
            system_bus=FakeSystemBus(owner=":1.99", owner_uid=2000))
        with self.assertLogs("panasonic_posture.session.agent", level="ERROR"):
            self.assertEqual(agent.serve(), 1)
        self.assertNotIn("run", events)

    def test_unknown_name_owner_uid_fails_conservatively(self):
        events = []
        uid_error = GLib.Error.new_literal(
            domain=Gio.dbus_error_quark(),
            code=Gio.DBusError.NAME_HAS_NO_OWNER,
            message="GDBus.Error:org.freedesktop.DBus.Error.NameHasNoOwner: gone",
        )

        def own_name(bus, name, flags, acquired, lost):
            events.append("own")
            lost(bus, name)
            return 7

        agent, _harness = self.build_agent(
            events, own_name,
            system_bus=FakeSystemBus(owner=":1.42", uid_error=uid_error))
        with self.assertLogs("panasonic_posture.session.agent", level="ERROR"):
            self.assertEqual(agent.serve(), 1)

    def test_losing_the_name_after_acquiring_exits_with_failure(self):
        events = []

        def own_name(bus, name, flags, acquired, lost):
            events.append("own")
            acquired(bus, name)
            lost(bus, name)
            return 7

        agent, _harness = self.build_agent(events, own_name)
        self.assertEqual(agent.serve(), 1)
        self.assertIn("quit", events)
        self.assertNotIn("run", events)


class NotificationActionTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(make_state())
        self.harness.notifier.notification = 42
        self.harness.notifier.epoch = 7

    def action(self, notification, name):
        self.harness.agent._on_action(
            None, None, None, None, None, GLib.Variant("(us)", (notification, name))
        )

    def test_matching_click_confirms_epoch_then_closes(self):
        self.action(42, "tent")
        self.assertEqual(self.harness.client.confirmations, [7])
        self.assertEqual(self.harness.notifier.close_calls, 1)
        self.assertEqual(self.harness.notifier.notification, 0)

    def test_default_body_click_confirms(self):
        self.action(42, "default")
        self.assertEqual(self.harness.client.confirmations, [7])

    def test_stand_notification_confirms_with_stand_method(self):
        self.harness.notifier.prompt = "stand"
        self.action(42, "default")
        self.assertEqual(self.harness.client.stand_confirmations, [7])
        self.assertEqual(self.harness.client.confirmations, [])
        self.assertEqual(self.harness.notifier.close_calls, 1)

    def test_old_notification_is_ignored(self):
        self.action(41, "tent")
        self.assertEqual(self.harness.client.confirmations, [])
        self.assertEqual(self.harness.notifier.close_calls, 0)

    def test_other_action_is_ignored(self):
        self.action(42, "open-settings")
        self.assertEqual(self.harness.client.confirmations, [])

    def test_expired_epoch_is_ignored(self):
        self.harness.notifier.epoch = None
        self.action(42, "tent")
        self.assertEqual(self.harness.client.confirmations, [])

    def test_runtime_failure_is_logged_not_propagated(self):
        runtime = self.harness.agent.runtime
        self.assertIsNotNone(runtime)
        runtime.handle_action = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertLogs("panasonic_posture.session.agent", level="ERROR"):
            self.action(42, "tent")


class SystemClientTests(unittest.TestCase):
    def test_state_is_validated(self):
        good = make_state().to_dict()
        client = SystemClient(FakeConnection(good))
        self.assertEqual(client.get_state().generation, 1)

    def test_old_state_version_is_rejected(self):
        client = SystemClient(FakeConnection([{"version": 1}]))
        with self.assertRaises(ServiceError) as caught:
            client.get_state()
        self.assertEqual(caught.exception.code, ErrorCode.INTERNAL)

    def test_dbus_error_is_split_into_stable_code(self):
        error = GLib.Error.new_literal(
            domain=Gio.dbus_error_quark(),
            code=Gio.DBusError.SERVICE_UNKNOWN,
            message=(
                "GDBus.Error:work.mmoe.panasonic.Posture.Error: "
                "display-transform-not-applied: readback-mismatch"
            ),
        )
        client = SystemClient(FakeConnection(error=error))
        with self.assertRaises(ServiceError) as caught:
            client.get_state()
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        self.assertEqual(caught.exception.detail, "readback-mismatch")
        self.assertEqual(caught.exception.name, protocol.DBUS_ERROR)

    def test_access_denied_keeps_the_dbus_error_name(self):
        error = GLib.Error.new_literal(
            domain=Gio.dbus_error_quark(),
            code=Gio.DBusError.ACCESS_DENIED,
            message="GDBus.Error:org.freedesktop.DBus.Error.AccessDenied: not allowed",
        )
        client = SystemClient(FakeConnection(error=error))
        with self.assertRaises(ServiceError) as caught:
            client.session_update(1, True, "", "")
        self.assertEqual(caught.exception.name, "org.freedesktop.DBus.Error.AccessDenied")
        self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        self.assertEqual(caught.exception.code, ErrorCode.INTERNAL)

    def test_session_update_uses_the_new_signature(self):
        connection = FakeConnection()
        client = SystemClient(connection)
        client.session_update(7, True, ErrorCode.DISPLAY_LOCK, "readback")
        call = connection.calls[-1]
        self.assertEqual(call[0], "SessionUpdate")
        self.assertEqual(call[1].get_type_string(), "(ubss)")
        self.assertEqual(
            call[1].unpack(), (7, True, ErrorCode.DISPLAY_LOCK, "readback")
        )

    def test_confirm_tent_sends_epoch(self):
        connection = FakeConnection()
        SystemClient(connection).confirm_tent(9)
        self.assertEqual(connection.calls[-1][0], "ConfirmTent")
        self.assertEqual(connection.calls[-1][1].unpack(), (9,))

    def test_confirm_stand_sends_epoch(self):
        connection = FakeConnection()
        SystemClient(connection).confirm_stand(9)
        self.assertEqual(connection.calls[-1][0], "ConfirmStand")
        self.assertEqual(connection.calls[-1][1].unpack(), (9,))

    def test_set_stand_prompt_sends_boolean(self):
        connection = FakeConnection()
        SystemClient(connection).set_stand_prompt(True)
        self.assertEqual(connection.calls[-1][0], "SetStandPrompt")
        self.assertEqual(connection.calls[-1][1].unpack(), (True,))

    def test_set_tent_enabled_sends_boolean(self):
        connection = FakeConnection()
        SystemClient(connection).set_tent_enabled(False)
        self.assertEqual(connection.calls[-1][0], "SetTentEnabled")
        self.assertEqual(connection.calls[-1][1].unpack(), (False,))


class FakeReply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeConnection:
    def __init__(self, state=None, error=None):
        self.state = state
        self.error = error
        self.calls = []

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable):
        self.calls.append((method, params))
        if self.error is not None:
            raise self.error
        if method == "GetState":
            import json

            return FakeReply((json.dumps(self.state),))
        return FakeReply(())


class SlowDisplayLeaseTests(unittest.TestCase):
    def make_runtime(self, *, lease_executor=None):
        self.client = FakeClient(make_state())
        self.display = FakeDisplay()
        self.executor = DeferredExecutor()
        runtime = SessionRuntime(
            self.client, self.display, None,
            executor=self.executor,
            lease_executor=lease_executor or InlineExecutor(),
        )
        runtime.tick(0.0)
        self.executor.run_next()
        runtime.tick(0.1)
        return runtime

    def finish_tasks(self, runtime, now):
        for _ in range(12):
            if not self.executor.calls:
                return
            self.executor.run_next()
            runtime.drain_completed_tasks(now)
        self.fail("session work did not settle")

    def test_slow_display_keeps_lease_without_acknowledging_success(self):
        runtime = self.make_runtime()
        for now in (0.7, 1.3, 1.9, 2.5, 3.1, 3.7):
            runtime.tick(now)
        self.assertGreaterEqual(len(self.client.updates), 5)
        self.assertTrue(all(update == (1, False, "", "") for update in self.client.updates))
        self.assertFalse(runtime.ready)
        self.assertEqual(self.display.applied, [])
        self.finish_tasks(runtime, 3.8)
        self.assertTrue(runtime.ready)
        self.assertEqual(self.client.updates[-1], (1, True, "", ""))

    def test_heartbeat_rejection_restores_after_inflight_display_finishes(self):
        runtime = self.make_runtime()
        self.client.update_error = service_error(ErrorCode.ACCESS_DENIED, "inactive")
        runtime.tick(0.7)
        self.assertTrue(runtime.failure_active)
        self.assertFalse(runtime.ready)
        self.finish_tasks(runtime, 0.8)
        self.assertEqual(self.display.restores, 1)
        self.assertFalse(runtime.ready)
        self.assertTrue(all(not update[1] for update in self.client.updates))

    def test_heartbeat_uses_current_target_and_stale_display_is_not_acknowledged(self):
        for change in ({"generation": 2}, {"instance": "instance-b"}):
            with self.subTest(change=change):
                runtime = self.make_runtime()
                self.client.state = make_state(
                    target=Target(mode="stand", tablet=True, rotation=0), **change
                )
                runtime.tick(0.7)
                self.assertEqual(self.client.updates[-1][0], self.client.state.generation)
                self.assertFalse(self.client.updates[-1][1])
                self.finish_tasks(runtime, 0.8)
                self.assertEqual(self.display.applied[-1].mode, "stand")
                self.assertEqual(runtime.instance, self.client.state.instance)
                self.assertEqual(runtime.generation, self.client.state.generation)
                self.assertEqual(sum(update[1] for update in self.client.updates), 1)

    def test_display_completion_waits_for_inflight_heartbeat_before_ready(self):
        leases = DeferredExecutor()
        runtime = self.make_runtime(lease_executor=leases)
        runtime.tick(0.7)
        self.assertEqual(len(leases.calls), 1)
        self.executor.run_next()
        runtime.tick(0.8)
        self.assertFalse(runtime.ready)
        self.assertEqual(self.executor.calls, [])
        leases.run_next()
        runtime.drain_completed_tasks(0.9)
        self.finish_tasks(runtime, 1.0)
        self.assertEqual(self.client.updates, [(1, False, "", ""), (1, True, "", "")])

    def test_continuous_heartbeat_failure_stops_slow_transaction(self):
        runtime = self.make_runtime()
        self.client.get_error = service_error(ErrorCode.INTERNAL, "transport timeout")
        for now in (0.7, 1.3, 1.9, 2.5, 3.1, 3.8):
            runtime.tick(now)
        self.assertTrue(runtime.failure_active)
        self.finish_tasks(runtime, 3.9)
        self.assertEqual(self.display.restores, 1)
        self.assertFalse(runtime.ready)


class HardwareHandoffTests(unittest.TestCase):
    def waiting(self):
        state = make_state(target=Target(mode="unavailable"))
        state.hardware = False
        state.posture_pending = True
        return state

    def test_native_reset_is_corrected_during_wait_before_ready(self):
        from test_display import Harness as DisplayHarness

        desktop = DisplayHarness(transform=2, locked=False)
        h = Harness(self.waiting(), display=desktop.display)
        h.tick(0.0)
        desktop.mutter.transform = 0
        h.tick(0.2)
        self.assertEqual(desktop.mutter.transform, 2)
        self.assertTrue(desktop.settings.value)
        self.assertTrue(all(not update[1] for update in h.client.updates))
        h.set_state(make_state(generation=2))
        h.tick(0.3)
        self.assertTrue(h.client.updates[-1][1])
        self.assertEqual(desktop.mutter.transforms, [2])

    def test_wait_rechecks_direction_without_extending_deadline(self):
        h = Harness(self.waiting())
        h.display.hold_orientation = mock.Mock()
        h.tick(0.0)
        h.tick(0.05)  # Completion must not spin an unbounded hold/read loop.
        h.display.hold_orientation.assert_called_once()
        h.tick(0.2)
        self.assertEqual(h.display.hold_orientation.call_count, 2)
        h.tick(1.9)
        self.assertEqual(h.display.hold_orientation.call_count, 3)
        self.assertTrue(all(not update[1] for update in h.client.updates))
        h.tick(2.01)
        h.tick(2.2)
        self.assertEqual(h.display.hold_orientation.call_count, 3)
        self.assertEqual([t.mode for t in h.display.applied], ["unavailable"])

    def test_wait_holds_without_ack_then_applies_stable_target(self):
        h = Harness(self.waiting())
        h.display.hold_orientation = mock.Mock()
        h.tick(0.0)
        h.tick(0.5)
        self.assertEqual(h.display.hold_orientation.call_count, 2)
        self.assertEqual(h.display.applied, [])
        self.assertFalse(h.client.updates[-1][1])
        h.set_state(make_state(generation=2))
        h.tick(0.8)
        self.assertEqual([t.mode for t in h.display.applied], ["tent"])
        self.assertTrue(h.client.updates[-1][1])

    def test_wait_expires_without_rearming_same_generation(self):
        h = Harness(self.waiting())
        h.display.hold_orientation = mock.Mock()
        for now in (0.0, 1.0, 2.1, 3.0):
            h.tick(now)
        self.assertEqual(h.display.hold_orientation.call_count, 2)
        self.assertEqual([t.mode for t in h.display.applied], ["unavailable"])

    def test_lost_sensor_or_hardware_cancels_wait_in_same_generation(self):
        for field, value in (("posture_pending", False), ("sensor_error", "lost"),
                             ("hardware", None), ("session_connected", False)):
            with self.subTest(field=field):
                h = Harness(self.waiting())
                h.display.hold_orientation = mock.Mock()
                h.tick(0.0)
                setattr(h.client.state, field, value)
                h.tick(0.1)
                self.assertEqual([t.mode for t in h.display.applied], ["unavailable"])

    def test_failed_hold_restores_instead_of_reporting_ready(self):
        h = Harness(self.waiting())
        h.display.hold_orientation = mock.Mock(side_effect=DisplayError(
            ErrorCode.DISPLAY_LOCK, "lock failed"))
        h.tick(0.0)
        self.assertFalse(h.client.updates[-1][1])
        h.tick(1.0)
        self.assertGreater(h.display.restores, 0)

    def test_shutdown_and_rejected_lease_restore_during_hold(self):
        for reject in (False, True):
            with self.subTest(reject=reject):
                h = Harness(self.waiting())
                h.display.hold_orientation = mock.Mock()
                h.tick(0.0)
                if reject:
                    h.client.update_error = service_error(ErrorCode.ACCESS_DENIED, "inactive")
                    h.tick(0.7)
                    self.assertTrue(h.agent.runtime.failure_active)
                else:
                    h.agent.runtime.close()
                self.assertGreater(h.display.restores, 0)
                self.assertFalse(h.agent.ready)

    def test_slow_hold_reads_new_target_before_any_ready_report(self):
        executor = DeferredExecutor()
        h = Harness(self.waiting(), executor=executor)
        h.display.hold_orientation = mock.Mock()
        h.tick(0.0)
        executor.run_next()  # GetState
        h.tick(0.1)  # hold queued
        h.set_state(make_state(generation=2, instance="new-daemon"))
        for _ in range(10):
            if not executor.calls:
                break
            executor.run_next()
            h.agent.runtime.drain_completed_tasks(0.8)
        self.assertEqual([t.mode for t in h.display.applied], ["tent"])
        self.assertTrue(h.client.updates[-1][1])
        self.assertTrue(all(not ready for gen, ready, _, _ in h.client.updates if gen == 1))


if __name__ == "__main__":
    unittest.main()
