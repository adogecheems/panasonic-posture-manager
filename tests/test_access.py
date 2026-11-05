"""Tests for :mod:`panasonic_posture.service.access` (fake D-Bus)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from gi.repository import Gio, GLib

from panasonic_posture.protocol import IFACE, NAME, PATH, session_name
from panasonic_posture.service.access import (
    LOGIND_TIMEOUT_MS,
    SESSION_UNIT,
    AccessControl,
    AccessDenied,
    SessionInfo,
)
from panasonic_posture.tasks import InlineExecutor


@dataclass
class Call:
    destination: str
    path: str
    interface: str
    method: str
    parameters: object
    timeout_ms: int

    def unpacked(self):
        return self.parameters.unpack() if self.parameters is not None else None


class FakeBus:
    """Just enough of ``Gio.DBusConnection`` for AccessControl."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.replies: dict[str, object] = {}
        self.paths: dict[str, str] = {}

    def set(self, method: str, reply: object) -> None:
        self.replies[method] = reply

    def set_active_session(self, *, path: str, reply_signature: str, value: object) -> None:
        self.paths[path] = path
        self.replies["GetAll"] = GLib.Variant(reply_signature, value)

    def call_sync(
        self,
        destination: str,
        path: str,
        interface: str,
        method: str,
        parameters: object,
        reply_type: object,
        flags: int,
        timeout_msec: int,
        cancellable: object = None,
    ) -> object:
        self.calls.append(
            Call(destination, path, interface, method, parameters, timeout_msec)
        )
        if method not in self.replies:
            raise AssertionError(f"unexpected D-Bus call {method}")
        reply = self.replies[method]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def methods(self) -> list[str]:
        return [call.method for call in self.calls]

    def call_for(self, method: str) -> Call:
        matches = [call for call in self.calls if call.method == method]
        if not matches:
            raise AssertionError(f"no call to {method}")
        return matches[-1]


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def gerror(message: str = "no such name") -> GLib.Error:
    return GLib.Error.new_literal(
        Gio.DBusError.quark(), message, Gio.DBusError.NAME_HAS_NO_OWNER
    )


def sessions_reply(*rows: tuple[str, int, str, str, str]) -> GLib.Variant:
    return GLib.Variant("(a(susso))", (list(rows),))


def session_properties(**values: object) -> GLib.Variant:
    return GLib.Variant(
        "(a{sv})",
        (
            {
                key: GLib.Variant(
                    "s" if isinstance(value, str) else "b", value
                )
                for key, value in values.items()
            },
        ),
    )


UI_UID = 1000
SESSION_PATH = "/org/freedesktop/login1/session/c1"


class CallerIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = FakeBus()
        self.access = AccessControl(self.bus)

    def test_caller_uid(self) -> None:
        self.bus.set("GetConnectionUnixUser", GLib.Variant("(u)", (UI_UID,)))
        self.assertEqual(self.access.caller_uid(":1.42"), UI_UID)
        call = self.bus.call_for("GetConnectionUnixUser")
        self.assertEqual(call.destination, "org.freedesktop.DBus")
        self.assertEqual(call.unpacked(), (":1.42",))

    def test_caller_uid_rejects_missing_sender_without_bus(self) -> None:
        with self.assertRaises(AccessDenied):
            self.access.caller_uid("")
        self.assertEqual(self.bus.calls, [])

    def test_caller_uid_bus_error_is_denied(self) -> None:
        self.bus.set("GetConnectionUnixUser", gerror("unknown name"))
        with self.assertRaises(AccessDenied):
            self.access.caller_uid(":1.42")

    def test_no_bus_connection_is_denied(self) -> None:
        access = AccessControl(None)
        for call in (
            lambda: access.caller_uid(":1.1"),
            lambda: access.session_name_owner(session_name(UI_UID)),
            lambda: access.active_session(UI_UID),
        ):
            with self.assertRaises(AccessDenied):
                call()

    def test_session_name_owner(self) -> None:
        name = session_name(UI_UID)
        self.bus.set("GetNameOwner", GLib.Variant("(s)", (":1.77",)))
        self.assertEqual(self.access.session_name_owner(name), ":1.77")
        call = self.bus.call_for("GetNameOwner")
        self.assertEqual(call.unpacked(), (name,))
        self.assertIn(".Session.u1000", name)
        self.assertEqual(call.timeout_ms, 800)

    def test_session_name_owner_explicit_name(self) -> None:
        self.bus.set("GetNameOwner", GLib.Variant("(s)", (":1.5",)))
        self.assertEqual(self.access.session_name_owner("a.b.C"), ":1.5")
        self.assertEqual(self.bus.call_for("GetNameOwner").unpacked(), ("a.b.C",))

    def test_session_name_owner_missing_name_is_none(self) -> None:
        self.bus.set("GetNameOwner", gerror())
        self.assertIsNone(self.access.session_name_owner(session_name(UI_UID)))

    def test_attach_swaps_bus_and_clears_cache(self) -> None:
        first = FakeBus()
        first.set("GetConnectionUnixUser", GLib.Variant("(u)", (UI_UID,)))
        access = AccessControl(first)
        self.assertEqual(access.caller_uid(":1.1"), UI_UID)
        second = FakeBus()
        second.set("GetConnectionUnixUser", GLib.Variant("(u)", (2000,)))
        access.attach(second)
        self.assertEqual(access.caller_uid(":1.1"), 2000)
        self.assertEqual(first.calls[0].method, "GetConnectionUnixUser")


class ActiveSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = FakeBus()
        self.access = AccessControl(self.bus)

    def _set(self, *rows: tuple[str, int, str, str, str], **properties: object) -> None:
        self.bus.set("ListSessions", sessions_reply(*rows))
        self.bus.set(
            "GetAll",
            session_properties(
                Active=properties.get("Active", True),
                Remote=properties.get("Remote", False),
                Type=properties.get("Type", "wayland"),
            ),
        )

    def test_finds_active_wayland_session(self) -> None:
        self._set(("c2", 1001, "other", "seat0", "/session/c2"), ("c1", UI_UID, "u", "seat0", SESSION_PATH))
        session = self.access.active_session(UI_UID)
        self.assertEqual(
            session,
            SessionInfo(
                uid=UI_UID, session_id="c1", path=SESSION_PATH, type="wayland"
            ),
        )
        self.assertEqual(self.bus.methods().count("GetAll"), 1)
        self.assertEqual(self.bus.call_for("GetAll").path, SESSION_PATH)
        self.assertEqual(
            self.bus.call_for("GetAll").unpacked(),
            ("org.freedesktop.login1.Session",),
        )

    def test_other_uid_and_seat_are_ignored(self) -> None:
        self._set(("c2", 1001, "other", "seat0", "/session/c2"), ("c3", UI_UID, "u", "seat1", "/session/c3"))
        with self.assertRaises(AccessDenied):
            self.access.active_session(UI_UID)
        self.assertEqual(self.bus.methods(), ["ListSessions"])

    def test_inactive_remote_and_non_graphical_are_ignored(self) -> None:
        for properties in (
            {"Active": False},
            {"Remote": True},
            {"Type": "tty"},
            {"Type": "unspecified"},
        ):
            with self.subTest(properties=properties):
                self.bus.calls.clear()
                self._set(("c1", UI_UID, "u", "seat0", SESSION_PATH), **properties)
                with self.assertRaises(AccessDenied):
                    self.access.active_session(UI_UID)

    def test_x11_session_is_denied(self) -> None:
        self._set(("c1", UI_UID, "u", "seat0", SESSION_PATH), Type="x11")
        with self.assertRaises(AccessDenied):
            self.access.active_session(UI_UID)

    def test_no_sessions_is_denied(self) -> None:
        self._set()
        with self.assertRaises(AccessDenied):
            self.access.active_session(UI_UID)

    def test_logind_error_is_denied(self) -> None:
        self.bus.set("ListSessions", gerror("logind unavailable"))
        with self.assertRaises(AccessDenied):
            self.access.active_session(UI_UID)


class AuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = FakeBus()
        self.clock = FakeClock()
        self.access = AccessControl(self.bus, monotonic=self.clock)
        self.bus.set("GetConnectionUnixUser", GLib.Variant("(u)", (UI_UID,)))
        self.bus.set(
            "ListSessions", sessions_reply(("c1", UI_UID, "u", "seat0", SESSION_PATH))
        )
        self.bus.set(
            "GetAll",
            session_properties(Active=True, Remote=False, Type="wayland"),
        )
        self.bus.set("CheckAuthorization", GLib.Variant("((bba{ss}))", ((True, False, {}),)))

    def test_authorized_caller_returns_uid(self) -> None:
        self.assertEqual(self.access.authorize(":1.9"), UI_UID)

    def test_polkit_request_uses_system_bus_name_subject(self) -> None:
        self.access.authorize(":1.9")
        call = self.bus.call_for("CheckAuthorization")
        self.assertEqual(call.destination, "org.freedesktop.PolicyKit1")
        self.assertEqual(call.interface, "org.freedesktop.PolicyKit1.Authority")
        unpacked = call.unpacked()
        self.assertEqual(unpacked[0][0], "system-bus-name")
        self.assertEqual(unpacked[0][1]["name"], ":1.9")
        self.assertEqual(unpacked[1], "work.mmoe.panasonic.Posture.configure")
        self.assertEqual(call.timeout_ms, 1500)

    def test_successful_authorization_is_cached_for_two_seconds(self) -> None:
        self.access.authorize(":1.9")
        count = len(self.bus.calls)
        self.clock.value += 1.9
        self.access.authorize(":1.9")
        self.assertEqual(len(self.bus.calls), count)
        self.clock.value += 0.2  # now 2.1 s after the first success
        self.access.authorize(":1.9")
        self.assertGreater(len(self.bus.calls), count)

    def test_negative_authorization_is_not_cached(self) -> None:
        self.bus.set("CheckAuthorization", GLib.Variant("((bba{ss}))", ((False, False, {}),)))
        for _ in range(2):
            with self.assertRaises(AccessDenied):
                self.access.authorize(":1.9")
        checks = [call for call in self.bus.calls if call.method == "CheckAuthorization"]
        self.assertEqual(len(checks), 2)

    def test_active_session_failure_is_denied(self) -> None:
        self.bus.set("ListSessions", sessions_reply())
        with self.assertRaises(AccessDenied):
            self.access.authorize(":1.9")
        self.assertNotIn("CheckAuthorization", self.bus.methods())

    def test_invalidate_one_sender(self) -> None:
        self.access.authorize(":1.9")
        self.access.invalidate(":1.9")
        count = len(self.bus.calls)
        self.assertEqual(self.access.authorize(":1.9"), UI_UID)
        self.assertGreater(len(self.bus.calls), count)

    def test_invalidate_all(self) -> None:
        self.access.authorize(":1.9")
        self.access.invalidate()
        count = len(self.bus.calls)
        self.access.authorize(":1.9")
        self.assertGreater(len(self.bus.calls), count)


class SessionAdapterVerificationTests(unittest.TestCase):
    """Four-step SessionUpdate identity chain (review Q-01)."""

    SENDER = ":1.42"
    PID = 4242
    UI_UID_2 = 1000

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.proc_root = Path(self._directory.name)
        self.clock = FakeClock()
        self.bus = FakeBus()
        self.access = AccessControl(
            self.bus,
            monotonic=self.clock,
            proc_root=self.proc_root,
            session_cache_seconds=1.0,
        )
        self.bus.set("GetConnectionUnixUser", GLib.Variant("(u)", (self.UI_UID_2,)))
        self.bus.set("GetConnectionUnixProcessID", GLib.Variant("(u)", (self.PID,)))
        self.bus.set("GetNameOwner", GLib.Variant("(s)", (self.SENDER,)))
        self.bus.set(
            "ListSessions",
            sessions_reply(("c1", self.UI_UID_2, "u", "seat0", SESSION_PATH)),
        )
        self.bus.set(
            "GetAll",
            session_properties(Active=True, Remote=False, Type="wayland"),
        )
        self.write_cgroup()

    def write_cgroup(self, *, pid: int | None = None, content: str | None = None) -> None:
        directory = self.proc_root / str(self.PID if pid is None else pid)
        directory.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = (
                "0::/user.slice/user-1000.slice/user@1000.service/app.slice/"
                f"{SESSION_UNIT}\n"
            )
        (directory / "cgroup").write_text(content, encoding="utf-8")

    def test_verify_returns_uid_after_all_steps(self) -> None:
        self.assertEqual(self.access.verify_session_adapter(self.SENDER), self.UI_UID_2)
        methods = self.bus.methods()
        self.assertEqual(methods[0], "GetConnectionUnixUser")
        self.assertEqual(methods[1], "GetConnectionUnixProcessID")
        self.assertIn("GetNameOwner", methods)
        self.assertIn("ListSessions", methods)
        owner_call = self.bus.call_for("GetNameOwner")
        self.assertEqual(owner_call.unpacked(), (session_name(self.UI_UID_2),))
        self.assertEqual(owner_call.timeout_ms, LOGIND_TIMEOUT_MS)
        self.assertEqual(LOGIND_TIMEOUT_MS, 800)
        pid_call = self.bus.call_for("GetConnectionUnixProcessID")
        self.assertEqual(pid_call.unpacked(), (self.SENDER,))

    def test_identity_is_cached_per_sender(self) -> None:
        self.access.verify_session_adapter(self.SENDER)
        self.access.verify_session_adapter(self.SENDER)
        self.assertEqual(self.bus.methods().count("GetConnectionUnixUser"), 1)
        self.assertEqual(self.bus.methods().count("GetConnectionUnixProcessID"), 1)
        # Ownership and the active session are checked again (name can move,
        # session can become inactive), but the logind session is cached <=1 s.
        self.assertEqual(self.bus.methods().count("GetNameOwner"), 2)
        self.assertEqual(self.bus.methods().count("ListSessions"), 1)

    def test_session_cache_expires_after_one_second(self) -> None:
        self.access.verify_session_adapter(self.SENDER)
        self.clock.value += 1.05
        self.access.verify_session_adapter(self.SENDER)
        self.assertEqual(self.bus.methods().count("ListSessions"), 2)

    def test_wrong_name_owner_is_denied(self) -> None:
        self.bus.set("GetNameOwner", GLib.Variant("(s)", (":1.99",)))
        with self.assertRaises(AccessDenied):
            self.access.verify_session_adapter(self.SENDER)

    def test_missing_name_owner_is_denied(self) -> None:
        self.bus.set("GetNameOwner", gerror())
        with self.assertRaises(AccessDenied):
            self.access.verify_session_adapter(self.SENDER)

    def test_inactive_session_is_denied(self) -> None:
        self.bus.set("ListSessions", sessions_reply())
        with self.assertRaises(AccessDenied):
            self.access.verify_session_adapter(self.SENDER)

    def test_session_type_not_graphical_is_denied(self) -> None:
        self.bus.set(
            "GetAll", session_properties(Active=True, Remote=False, Type="tty")
        )
        with self.assertRaises(AccessDenied):
            self.access.verify_session_adapter(self.SENDER)

    def test_process_outside_session_unit_is_denied(self) -> None:
        self.write_cgroup(content="0::/user.slice/user-1000.slice/session-2.scope\n")
        with self.assertRaises(AccessDenied):
            self.access.verify_session_adapter(self.SENDER)

    def test_lookalike_and_delegated_cgroups_are_denied(self) -> None:
        manager = "/user.slice/user-1000.slice/user@1000.service/app.slice"
        for cgroup in (
            f"0::{manager}/fake-{SESSION_UNIT}",
            f"0::{manager}/other.service/{SESSION_UNIT}",
            f"0::/delegated.service{manager}/{SESSION_UNIT}",
            f"5:cpu:{manager}/{SESSION_UNIT}",
            f"0::{manager.replace('user@1000', 'user@1001')}/{SESSION_UNIT}",
        ):
            with self.subTest(cgroup=cgroup):
                self.write_cgroup(content=f"{cgroup}\n")
                with self.assertRaises(AccessDenied):
                    self.access.verify_session_adapter(self.SENDER)

    def test_legacy_systemd_hierarchy_is_accepted(self) -> None:
        self.write_cgroup(content=(
            "5:name=systemd:/user.slice/user-1000.slice/user@1000.service/"
            f"app.slice/{SESSION_UNIT}\n"
        ))
        self.assertEqual(self.access.verify_session_adapter(self.SENDER), self.UI_UID_2)

    def test_unreadable_cgroup_is_denied(self) -> None:
        # No cgroup file at all: fails closed.
        self.write_cgroup(content=None)
        (self.proc_root / str(self.PID) / "cgroup").unlink(missing_ok=True)
        with self.assertRaises(AccessDenied):
            self.access.verify_session_adapter(self.SENDER)

    def test_missing_pid_is_denied(self) -> None:
        self.bus.set("GetConnectionUnixProcessID", gerror("no such connection"))
        with self.assertRaises(AccessDenied):
            self.access.verify_session_adapter(self.SENDER)

    def test_active_session_cached_positive(self) -> None:
        first = self.access.active_session_cached(self.UI_UID_2)
        self.assertEqual(first.uid, self.UI_UID_2)
        self.access.active_session_cached(self.UI_UID_2)
        self.assertEqual(self.bus.methods().count("ListSessions"), 1)
        self.clock.value += 1.05
        self.access.active_session_cached(self.UI_UID_2)
        self.assertEqual(self.bus.methods().count("ListSessions"), 2)


class DaemonAuthBoundaryTests(unittest.TestCase):
    """Daemon-side SessionUpdate authentication + method dispatch (Q-01/Q-02/Q-06).

    The daemon object is built with ``__new__`` and a fake application so no
    D-Bus connection, logind or root is needed.  Keeping the fake behind the
    same composition boundary as production prevents the shell from growing
    hidden dependencies on individual collaborators.
    """

    class Invocation:
        def __init__(self) -> None:
            self.value = None
            self.error: tuple[str, str] | None = None

        def return_value(self, variant) -> None:
            self.value = variant

        def return_dbus_error(self, name: str, message: str) -> None:
            self.error = (name, message)

    def make_daemon(self, *, development: bool = False, verify_error=None):
        from panasonic_posture.service.daemon import Daemon

        daemon = Daemon.__new__(Daemon)
        daemon.development = development
        daemon.stop = threading.Event()
        daemon._authorization_workers = {
            "request": InlineExecutor(),
            "session": InlineExecutor(),
        }
        daemon._authorization_pending = {"request": 0, "session": 0}
        daemon._exit_code = 0
        daemon._name_acquired_ok = False
        daemon.connection = None
        daemon.registration = None
        access = mock.Mock()
        access.authorize.return_value = 1000
        if verify_error is not None:
            access.verify_session_adapter.side_effect = verify_error
        else:
            access.verify_session_adapter.return_value = 1000
        engine = mock.Mock()
        engine.state_json.return_value = '{"version": 2}'
        daemon.application = SimpleNamespace(
            access=access,
            engine=engine,
            start=mock.Mock(),
            sleep_event=mock.Mock(),
        )
        daemon._tick = mock.Mock()
        daemon._schedule_tick = mock.Mock()
        daemon._tick_idle_id = None
        return daemon

    def call(self, daemon, method: str, parameters=None, *, dispatch_idle=True):
        invocation = self.Invocation()

        def dispatch():
            daemon._method(
                None,
                ":1.42",
                PATH,
                IFACE,
                method,
                parameters if parameters is not None else GLib.Variant("()", ()),
                invocation,
            )

        if dispatch_idle:
            with mock.patch.object(
                GLib, "idle_add", side_effect=lambda callback, *args, **_kwargs: callback(*args)
            ):
                dispatch()
        else:
            dispatch()
        return invocation

    def test_get_state_is_a_cached_snapshot_without_tick(self) -> None:
        daemon = self.make_daemon()
        invocation = self.call(daemon, "GetState")
        daemon._tick.assert_not_called()
        daemon._schedule_tick.assert_not_called()
        self.assertIsNotNone(invocation.value)
        self.assertIsNone(invocation.error)

    def test_follow_up_tick_is_coalesced_before_queued_bus_reads(self) -> None:
        from panasonic_posture.service import daemon as daemon_module

        daemon = daemon_module.Daemon.__new__(daemon_module.Daemon)
        daemon._tick_idle_id = None
        daemon._tick = mock.Mock()
        with mock.patch.object(
            daemon_module.GLib, "idle_add", return_value=77
        ) as idle_add:
            daemon._schedule_tick()
            daemon._schedule_tick()
        idle_add.assert_called_once()
        self.assertEqual(
            idle_add.call_args.kwargs["priority"], daemon_module.GLib.PRIORITY_HIGH
        )
        self.assertEqual(daemon._tick_idle_id, 77)
        self.assertEqual(
            daemon._run_scheduled_tick(), daemon_module.GLib.SOURCE_REMOVE
        )
        daemon._tick.assert_called_once_with()
        self.assertIsNone(daemon._tick_idle_id)

    def test_retry_requires_authorization_and_dispatches(self) -> None:
        daemon = self.make_daemon()
        invocation = self.call(daemon, "Retry")
        daemon.application.access.authorize.assert_called_once_with(":1.42")
        daemon.application.engine.retry.assert_called_once_with()
        daemon._tick.assert_not_called()
        daemon._schedule_tick.assert_called_once()
        self.assertIsNone(invocation.error)

    def test_pending_user_authorization_leaves_state_and_heartbeats_responsive(self) -> None:
        daemon = self.make_daemon()
        future = Future()
        worker = mock.Mock()
        worker.submit.return_value = future
        daemon._authorization_workers["request"] = worker

        pending = self.call(daemon, "Retry", dispatch_idle=False)
        self.assertIsNone(pending.value)
        daemon.application.access.authorize.assert_not_called()
        daemon.application.engine.retry.assert_not_called()
        self.assertIsNotNone(self.call(daemon, "GetState").value)
        heartbeat = self.call(
            daemon, "SessionUpdate", GLib.Variant("(ubss)", (7, True, "", ""))
        )
        self.assertIsNotNone(heartbeat.value)
        daemon.application.engine.session_update.assert_called_once_with(
            7, True, "", "", 1000
        )

        with mock.patch.object(GLib, "idle_add") as idle:
            future.set_result(1000)
        daemon.application.engine.retry.assert_not_called()
        callback, *args = idle.call_args.args
        callback(*args)
        self.assertIsNotNone(pending.value)
        daemon.application.engine.retry.assert_called_once_with()

    def test_shutdown_drops_a_completed_authorization(self) -> None:
        daemon = self.make_daemon()
        future = Future()
        worker = mock.Mock()
        worker.submit.return_value = future
        daemon._authorization_workers["request"] = worker
        self.call(daemon, "Retry", dispatch_idle=False)
        with mock.patch.object(GLib, "idle_add") as idle:
            future.set_result(1000)
        daemon.stop.set()
        callback, *args = idle.call_args.args
        callback(*args)
        daemon.application.engine.retry.assert_not_called()

    def test_full_authorization_queue_rejects_new_requests(self) -> None:
        from panasonic_posture.service.daemon import MAX_PENDING_AUTHORIZATIONS

        daemon = self.make_daemon()
        worker = mock.Mock()
        daemon._authorization_workers["session"] = worker
        daemon._authorization_pending["session"] = MAX_PENDING_AUTHORIZATIONS

        response = self.call(
            daemon, "SessionUpdate", GLib.Variant("(ubss)", (7, True, "", ""))
        )

        self.assertIn("authorization-busy", response.error[1])
        worker.submit.assert_not_called()
        daemon.application.engine.session_update.assert_not_called()
        self.assertIsNotNone(self.call(daemon, "Retry").value)

    def test_session_update_uses_verified_adapter_uid(self) -> None:
        daemon = self.make_daemon()
        invocation = self.call(
            daemon, "SessionUpdate", GLib.Variant("(ubss)", (7, True, "", ""))
        )
        daemon.application.access.verify_session_adapter.assert_called_once_with(":1.42")
        daemon.application.engine.session_update.assert_called_once_with(
            7, True, "", "", 1000
        )
        self.assertIsNone(invocation.error)

    def test_session_update_auth_failure_maps_access_denied(self) -> None:
        daemon = self.make_daemon(verify_error=AccessDenied("not active"))
        invocation = self.call(
            daemon, "SessionUpdate", GLib.Variant("(ubss)", (7, True, "", ""))
        )
        self.assertIsNotNone(invocation.error)
        name, message = invocation.error
        from panasonic_posture.protocol import DBUS_ERROR

        self.assertEqual(name, DBUS_ERROR)
        self.assertTrue(message.startswith("access-denied"), message)
        daemon.application.engine.session_update.assert_not_called()

    def test_session_update_rejected_before_engine_lease_refresh(self) -> None:
        daemon = self.make_daemon(verify_error=AccessDenied("inactive session"))
        self.call(
            daemon, "SessionUpdate", GLib.Variant("(ubss)", (7, True, "", ""))
        )
        daemon.application.engine.session_update.assert_not_called()
        daemon._tick.assert_not_called()
        daemon._schedule_tick.assert_not_called()

    def test_name_lost_after_acquisition_is_failure_exit(self) -> None:
        daemon = self.make_daemon()
        daemon.loop = mock.Mock()
        daemon._name_acquired_ok = True
        daemon._exit_code = 0
        daemon._name_lost(None, NAME)
        daemon.loop.quit.assert_called_once()
        self.assertEqual(daemon._exit_code, 1)

    def test_duplicate_name_before_acquisition_is_clean_exit(self) -> None:
        daemon = self.make_daemon()
        daemon.loop = mock.Mock()
        bus = FakeBus()
        bus.set("NameHasOwner", GLib.Variant("(b)", (True,)))
        daemon._name_lost(bus, NAME)
        daemon.loop.quit.assert_called_once()
        self.assertEqual(daemon._exit_code, 0)
        call = bus.call_for("NameHasOwner")
        self.assertEqual(call.destination, "org.freedesktop.DBus")
        self.assertEqual(call.unpacked(), (NAME,))
        self.assertEqual(call.timeout_ms, 500)

    def test_unavailable_bus_before_acquisition_is_failure_exit(self) -> None:
        daemon = self.make_daemon()
        daemon.loop = mock.Mock()
        daemon._name_lost(None, NAME)
        daemon.loop.quit.assert_called_once()
        self.assertEqual(daemon._exit_code, 1)

    def test_unowned_or_unverifiable_name_is_failure_exit(self) -> None:
        for reply in (GLib.Variant("(b)", (False,)), gerror("bus disconnected")):
            with self.subTest(reply=reply):
                daemon = self.make_daemon()
                daemon.loop = mock.Mock()
                bus = FakeBus()
                bus.set("NameHasOwner", reply)
                daemon._name_lost(bus, NAME)
                daemon.loop.quit.assert_called_once()
                self.assertEqual(daemon._exit_code, 1)

    def test_duplicate_name_does_not_clear_an_initialization_failure(self) -> None:
        daemon = self.make_daemon()
        daemon.loop = mock.Mock()
        daemon._exit_code = 1
        bus = FakeBus()
        bus.set("NameHasOwner", GLib.Variant("(b)", (True,)))
        daemon._name_lost(bus, NAME)
        daemon.loop.quit.assert_called_once()
        self.assertEqual(daemon._exit_code, 1)

    def test_bus_acquired_failure_sets_failure_exit(self) -> None:
        daemon = self.make_daemon()
        daemon.loop = mock.Mock()
        daemon.connection = None
        daemon.registration = None
        daemon._exit_code = 0
        daemon.application.access.attach = mock.Mock()
        connection = mock.Mock()
        connection.register_object.side_effect = RuntimeError("boom")
        daemon._bus_acquired(connection, NAME)
        self.assertEqual(daemon._exit_code, 1)
        daemon.loop.quit.assert_called_once()
        daemon.application.start.assert_not_called()

    def test_bus_acquired_success_starts_engine_and_registers_object(self) -> None:
        daemon = self.make_daemon(development=True)
        daemon.loop = mock.Mock()
        daemon.connection = None
        daemon.registration = None
        daemon.application.access.attach = mock.Mock()
        connection = mock.Mock()
        connection.register_object.return_value = 42
        daemon._bus_acquired(connection, NAME)
        daemon.application.access.attach.assert_called_once_with(connection)
        self.assertEqual(daemon.registration, 42)
        daemon.application.start.assert_called_once()
        daemon._tick.assert_called_once()
        self.assertEqual(daemon._exit_code, 0)

    def test_development_mode_skips_all_identity_checks(self) -> None:
        daemon = self.make_daemon(development=True)
        invocation = self.call(
            daemon, "SessionUpdate", GLib.Variant("(ubss)", (7, True, "", ""))
        )
        daemon.application.access.verify_session_adapter.assert_not_called()
        daemon.application.engine.session_update.assert_called_once_with(
            7, True, "", "", 0
        )
        self.assertIsNone(invocation.error)


if __name__ == "__main__":
    unittest.main()
