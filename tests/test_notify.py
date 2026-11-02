"""Tests for the tent-prompt notification state machine (fake notification bus)."""

import unittest

from gi.repository import Gio, GLib

from panasonic_posture.session.notify import RETRY_DELAYS, Notifier


class FakeReply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeNotificationBus:
    def __init__(self, capabilities=("actions", "body")):
        self.calls = []
        self.capabilities = list(capabilities)
        self.fail = {}
        self.next_id = 100

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable):
        self.calls.append((method, params))
        failures = self.fail.get(method, 0)
        if failures:
            self.fail[method] = failures - 1
            raise GLib.Error.new_literal(
                domain=Gio.dbus_error_quark(),
                code=Gio.DBusError.SERVICE_UNKNOWN,
                message="GDBus.Error:org.freedesktop.Notifications.Error: unavailable",
            )
        if method == "GetCapabilities":
            return FakeReply((self.capabilities,))
        if method == "Notify":
            self.next_id += 1
            return FakeReply((self.next_id,))
        if method == "CloseNotification":
            return FakeReply(())
        raise AssertionError(method)

    def methods(self):
        return [method for method, _params in self.calls]


class Ticker:
    """Deterministic monotonic clock."""

    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class NotifierTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeNotificationBus()
        self.clock = Ticker()
        self.notifier = Notifier(
            self.bus, lambda text: text, clock=self.clock, timeout_ms=10
        )
        self.notifications = []

    # -- successes --------------------------------------------------------

    def test_successful_notify_records_shown_epoch_only_after_call(self):
        self.assertIsNone(self.notifier.shown_epoch)
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.notifier.shown_epoch, 7)
        self.assertEqual(self.notifier.epoch, 7)
        self.assertEqual(self.notifier.notification, 101)
        self.assertIn("Notify", self.bus.methods())
        self.assertEqual(self.bus.methods()[-1], "Notify")

    def test_shown_epoch_is_not_notified_twice(self):
        self.notifier.sync(7, now=self.clock())
        self.clock.advance(10)
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.bus.methods().count("Notify"), 1)

    def test_notification_payload_carries_tent_action(self):
        self.notifier.sync(7, now=self.clock())
        method, parameters = self.bus.calls[-1]
        self.assertEqual(method, "Notify")
        (app_name, replaces, icon, summary, body, actions, hints, timeout) = parameters.unpack()
        self.assertEqual(replaces, 0)
        self.assertEqual(icon, "work.mmoe.panasonic.PostureManager")
        self.assertEqual(actions, ["default", "Enable tent mode"])
        self.assertIn("desktop-entry", hints)
        self.assertEqual(timeout, -1)
        self.assertTrue(app_name)
        self.assertTrue(summary)
        self.assertTrue(body)

    def test_capabilities_without_actions_skip_the_button(self):
        self.bus.capabilities = ["body"]
        self.notifier.sync(3, now=self.clock())
        _method, parameters = self.bus.calls[-1]
        self.assertEqual(parameters.unpack()[5], [])

    def test_new_epoch_replaces_old_notification(self):
        self.notifier.sync(7, now=self.clock())
        self.notifier.sync(8, now=self.clock())
        self.assertIn("CloseNotification", self.bus.methods())
        self.assertEqual(self.notifier.shown_epoch, 8)
        self.assertEqual(self.notifier.notification, 102)

    # -- failures and retries --------------------------------------------

    def test_failed_notification_is_not_recorded_and_retries_with_backoff(self):
        self.bus.fail["Notify"] = 10
        self.notifier.sync(7, now=self.clock())
        self.assertIsNone(self.notifier.shown_epoch)
        self.assertEqual(self.notifier.epoch, None)
        self.assertEqual(self.notifier.notification, 0)
        attempts = self.bus.methods().count("Notify")
        self.assertEqual(attempts, 1)

        # Before the backoff expires nothing happens.
        self.clock.advance(RETRY_DELAYS[0] / 2)
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.bus.methods().count("Notify"), 1)

        # After 1 s the second attempt is made; then 2 s, then 4 s.
        self.clock.advance(RETRY_DELAYS[0])
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.bus.methods().count("Notify"), 2)
        self.clock.advance(RETRY_DELAYS[1])
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.bus.methods().count("Notify"), 3)

    def test_retry_that_succeeds_records_shown_epoch(self):
        self.bus.fail["Notify"] = 1
        self.notifier.sync(7, now=self.clock())
        self.assertIsNone(self.notifier.shown_epoch)
        self.clock.advance(RETRY_DELAYS[0])
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.notifier.shown_epoch, 7)

    def test_retries_are_bounded_and_epoch_abandoned(self):
        self.bus.fail["Notify"] = 100
        times = [0.0]
        self.notifier.sync(7, now=times[-1])
        for delay in RETRY_DELAYS:
            times.append(times[-1] + delay)
            self.notifier.sync(7, now=times[-1])
        # Initial attempt + three retries.
        self.assertEqual(self.bus.methods().count("Notify"), 1 + len(RETRY_DELAYS))
        self.clock.advance(100)
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.bus.methods().count("Notify"), 1 + len(RETRY_DELAYS))

    def test_allow_retry_false_suppresses_retries(self):
        self.bus.fail["Notify"] = 1
        self.notifier.sync(7, now=self.clock(), allow_retry=False)
        self.clock.advance(10)
        self.notifier.sync(7, now=self.clock(), allow_retry=False)
        self.assertEqual(self.bus.methods().count("Notify"), 1)

    def test_new_epoch_resets_backoff(self):
        self.bus.fail["Notify"] = 1
        self.notifier.sync(7, now=self.clock())
        self.notifier.sync(8, now=self.clock())
        self.assertIn("Notify", self.bus.methods())
        self.assertEqual(self.bus.methods()[-1], "Notify")
        self.assertEqual(self.notifier.shown_epoch, 8)

    # -- close ------------------------------------------------------------

    def test_close_clears_epochs_and_allows_reprompt(self):
        self.notifier.sync(7, now=self.clock())
        self.notifier.close()
        self.assertEqual(self.notifier.notification, 0)
        self.assertIsNone(self.notifier.epoch)
        self.assertIsNone(self.notifier.shown_epoch)
        self.assertIn("CloseNotification", self.bus.methods())
        self.clock.advance(1)
        self.notifier.sync(7, now=self.clock())
        self.assertEqual(self.notifier.shown_epoch, 7)
        self.assertEqual(self.bus.methods().count("Notify"), 2)

    def test_sync_none_closes(self):
        self.notifier.sync(7, now=self.clock())
        self.notifier.sync(None, now=self.clock())
        self.assertIsNone(self.notifier.shown_epoch)
        self.assertEqual(self.notifier.notification, 0)
        self.assertIn("CloseNotification", self.bus.methods())

    def test_close_without_notification_is_safe(self):
        self.notifier.close()
        self.assertNotIn("CloseNotification", self.bus.methods())


if __name__ == "__main__":
    unittest.main()
