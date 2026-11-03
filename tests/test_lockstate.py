"""Tests for desktop-neutral screen-lock detection."""

import unittest

from gi.repository import Gio, GLib

from panasonic_posture.session.lockstate import (
    login1_locked,
    query_locked,
    screensaver_locked,
)


class Reply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


def dbus_error():
    return GLib.Error.new_literal(
        Gio.dbus_error_quark(),
        Gio.DBusError.SERVICE_UNKNOWN,
        "service unavailable",
    )


class LoginBus:
    def __init__(self, locked=True, fail=False):
        self.locked = locked
        self.fail = fail
        self.calls = []

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable):
        del path, iface, params, reply_type, flags, timeout, cancellable
        self.calls.append((dest, method))
        if self.fail:
            raise dbus_error()
        if method == "GetSession":
            return Reply(("/org/freedesktop/login1/session/_31",))
        if method == "Get":
            return Reply((GLib.Variant("b", self.locked),))
        raise AssertionError(method)


class ScreenSaverBus:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable):
        del path, iface, method, params, reply_type, flags, timeout, cancellable
        self.calls.append(dest)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return Reply((response,))


class LockStateTests(unittest.TestCase):
    def test_logind_locked_hint(self):
        self.assertTrue(login1_locked(LoginBus(locked=True)))
        self.assertFalse(login1_locked(LoginBus(locked=False)))
        self.assertIsNone(login1_locked(LoginBus(fail=True)))

    def test_freedesktop_screensaver(self):
        bus = ScreenSaverBus([True])
        self.assertTrue(screensaver_locked(bus))
        self.assertEqual(bus.calls, ["org.freedesktop.ScreenSaver"])

    def test_gnome_screensaver_fallback(self):
        bus = ScreenSaverBus([dbus_error(), True])
        self.assertTrue(screensaver_locked(bus))
        self.assertEqual(
            bus.calls,
            ["org.freedesktop.ScreenSaver", "org.gnome.ScreenSaver"],
        )

    def test_logind_wins_over_session_bus(self):
        session_bus = ScreenSaverBus([False])
        self.assertTrue(
            query_locked(system_bus=LoginBus(locked=True), session_bus=session_bus)
        )
        self.assertEqual(session_bus.calls, [])

    def test_logind_unavailable_falls_back_to_screensaver(self):
        session_bus = ScreenSaverBus([True])
        self.assertTrue(
            query_locked(system_bus=LoginBus(fail=True), session_bus=session_bus)
        )

    def test_unavailable_is_unlocked(self):
        self.assertFalse(query_locked(system_bus=LoginBus(fail=True), session_bus=None))
        self.assertFalse(query_locked())


if __name__ == "__main__":
    unittest.main()
