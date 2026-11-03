"""Tests for the session display transaction (fake Mutter bus / GSettings).

The pure planning helpers are tested directly; the transaction tests use fake
D-Bus/GSettings/journal objects so no real GNOME session is required.
"""

import unittest

from gi.repository import Gio, GLib

from panasonic_posture.model import Target
from panasonic_posture.protocol import ErrorCode
from panasonic_posture.session.display import (
    Display,
    DisplayError,
    desired_transform,
    internal_monitor,
    rotation_plan,
)


def _raise_dbus_error(*_args):
    raise dbus_error("org.freedesktop.DBus.Error.NoReply", "mutter gone")


def dbus_error(name, message="unavailable"):
    return GLib.Error.new_literal(
        domain=Gio.dbus_error_quark(),
        code=Gio.DBusError.SERVICE_UNKNOWN,
        message=f"GDBus.Error:{name}: {message}",
    )


def monitor_state(
    transform=0,
    serial=2,
    mirror=False,
    external=False,
    builtin=True,
    spec=("eDP-1", "AUO", "panel", "1"),
):
    inner = spec
    outer = ("HDMI-1", "Other", "screen", "2")
    modes = [("mode", 2880, 1920, 60.0, 2.0, [2.0], {"is-current": True})]
    monitors = [(inner, modes, {"is-builtin": True} if builtin else {})]
    if mirror or external:
        monitors.append((outer, modes, {}))
    logical = [(0, 0, 2.0, transform, True, [inner, outer] if mirror else [inner], {})]
    if external:
        logical.append((1440, 0, 2.0, 0, False, [outer], {}))
    return (serial, monitors, logical, {"layout-mode": 1})


class Reply:
    def __init__(self, value):
        self._value = value

    def unpack(self):
        return self._value


class FakeMutter:
    """Fake org.gnome.Mutter.DisplayConfig with an observable timeline."""

    def __init__(self, timeline, transform=0, fail_apply=0, apply_updates=True):
        self.timeline = timeline
        self.transform = transform
        self.monitor_spec = ("eDP-1", "AUO", "panel", "1")
        self.fail_apply = fail_apply
        self.apply_updates = apply_updates
        self.state_calls = 0
        self.apply_calls = 0
        self.transforms = []

    def call_sync(self, dest, path, iface, method, params, reply_type, flags, timeout, cancellable):
        self.timeline.append(method)
        if method == "GetCurrentState":
            self.state_calls += 1
            return Reply(
                monitor_state(
                    transform=self.transform,
                    serial=self.state_calls,
                    spec=self.monitor_spec,
                )
            )
        if method == "ApplyMonitorsConfig":
            self.apply_calls += 1
            if self.fail_apply:
                self.fail_apply -= 1
                raise dbus_error("org.freedesktop.DBus.Error.NoReply", "display busy")
            _serial, _method, rows, _props = params.unpack()
            self.transforms.append(rows[0][3])
            if self.apply_updates:
                self.transform = rows[0][3]
            return Reply(())
        raise AssertionError(f"unexpected Mutter call {method}")


class FakeSettings:
    """Fake Gio.Settings for org.gnome.settings-daemon.peripherals.touchscreen."""

    def __init__(self, timeline, locked=False, set_result=True, readback_override=None):
        self.timeline = timeline
        self.value = bool(locked)
        self.set_result = set_result
        self.readback_override = readback_override
        self.writes = []
        self.sync_calls = 0

    def get_boolean(self, key):
        if key != "orientation-lock":
            raise AssertionError(key)
        if self.readback_override is not None:
            return self.readback_override
        return self.value

    def set_boolean(self, key, value):
        if key != "orientation-lock":
            raise AssertionError(key)
        self.writes.append(bool(value))
        self.timeline.append(("lock", bool(value)))
        if not self.set_result:
            return False
        if self.readback_override is None:
            self.value = bool(value)
        return True

    def sync(self):
        self.sync_calls += 1


class FakeJournal:
    def __init__(self, timeline, document=None):
        self.timeline = timeline
        self.document = document
        self.saves = []

    def load(self):
        self.timeline.append("journal:load")
        return self.document

    def save(self, document):
        self.timeline.append("journal:save")
        self.saves.append(dict(document))
        self.document = dict(document)

    def clear(self):
        self.timeline.append("journal:clear")
        self.document = None


def owned_journal(transform=0, original_transform=0, original_lock=False):
    return {
        "version": 1,
        "spec": ["eDP-1", "AUO", "panel", "1"],
        "original_transform": original_transform,
        "original_lock": original_lock,
        "written_lock": False,
        "written_transform": transform,
    }


class Harness:
    def __init__(
        self,
        transform=0,
        locked=False,
        document=None,
        mutter_fail_state=False,
        settings_kwargs=None,
        **mutter_kwargs,
    ):
        self.timeline = []
        self.mutter = FakeMutter(self.timeline, transform=transform, **mutter_kwargs)
        if mutter_fail_state:
            self.mutter.call_sync = _raise_dbus_error
        self.settings = FakeSettings(self.timeline, locked=locked, **(settings_kwargs or {}))
        self.journal = FakeJournal(self.timeline, document)
        self.sleeps = []
        self.display = Display(
            self.mutter,
            self.settings,
            self.journal,
            lambda text: text,
            sleep=self.sleeps.append,
        )
        self.timeline.clear()


class PurePlanTests(unittest.TestCase):
    def test_only_internal_rotation_changes(self):
        state = monitor_state(external=True)
        spec, current = internal_monitor(state)
        serial, rows, properties = rotation_plan(state, spec, 2)
        self.assertEqual(current, 0)
        self.assertEqual(serial, 2)
        self.assertEqual(rows[0][3], 2)
        self.assertEqual(rows[1][:5], state[2][1][:5])
        self.assertEqual(rows[1][5][0][0], "HDMI-1")
        self.assertEqual(properties["layout-mode"].unpack(), 1)

    def test_mirror_is_rejected(self):
        with self.assertRaises(DisplayError) as caught:
            internal_monitor(monitor_state(mirror=True))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_MIRRORED)

    def test_missing_builtin_is_rejected(self):
        state = monitor_state(builtin=False)
        with self.assertRaises(DisplayError) as caught:
            internal_monitor(state)
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_IDENTITY)

    def test_portrait_external_layout_is_not_silently_rearranged(self):
        state = monitor_state(transform=1, external=True)
        spec, _ = internal_monitor(state)
        with self.assertRaises(DisplayError) as caught:
            rotation_plan(state, spec, 2)
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_LAYOUT)

    def test_single_monitor_portrait_to_landscape_is_valid(self):
        state = monitor_state(transform=1)
        spec, _ = internal_monitor(state)
        self.assertEqual(rotation_plan(state, spec, 2)[1][0][3], 2)

    def test_disconnected_internal_monitor_is_unavailable(self):
        state = monitor_state()
        with self.assertRaises(DisplayError) as caught:
            rotation_plan(state, ("DP-9", "X", "Y", "Z"), 0)
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)

    def test_software_mode_transforms(self):
        self.assertEqual(desired_transform("tent"), 2)
        self.assertEqual(desired_transform("stand"), 0)
        # Tablet starts in landscape even when leaving a 180 degree tent.
        self.assertEqual(desired_transform("tablet"), 0)
        self.assertEqual(desired_transform("laptop"), 0)

    def test_180_rotation_keeps_external_layout(self):
        state = monitor_state(external=True)
        spec, _ = internal_monitor(state)
        rows = rotation_plan(state, spec, 2)[1]
        self.assertEqual(rows[0][3], 2)
        self.assertEqual(rows[1][:5], state[2][1][:5])


class ApplyTransactionTests(unittest.TestCase):
    def test_tent_locks_then_rotates_and_journals_intent(self):
        harness = Harness(transform=0, locked=False)
        harness.display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(harness.mutter.transform, 2)
        self.assertTrue(harness.settings.value)
        self.assertLess(harness.timeline.index("journal:save"), harness.timeline.index(("lock", True)))
        self.assertLess(harness.timeline.index(("lock", True)), harness.timeline.index("ApplyMonitorsConfig"))
        document = harness.journal.document
        self.assertIsNotNone(document)
        self.assertEqual(document["backend"], "gnome")
        self.assertEqual(document["original_transform"], 0)
        self.assertEqual(document["original_lock"], False)
        self.assertTrue(document["written_lock"])
        self.assertEqual(document["written_transform"], 2)

    def test_tablet_rotates_while_lock_held_then_releases(self):
        # Coming from a locked tent state: rotate(0) must happen before unlock.
        harness = Harness(transform=2, locked=True)
        harness.display.apply(Target(mode="tablet", tablet=None, rotation=None, disable_internal_input=True))
        self.assertEqual(harness.mutter.transform, 0)
        self.assertFalse(harness.settings.value)
        apply_index = harness.timeline.index("ApplyMonitorsConfig")
        unlock_indices = [i for i, event in enumerate(harness.timeline) if event == ("lock", False)]
        self.assertTrue(unlock_indices)
        self.assertLess(apply_index, unlock_indices[-1])
        self.assertNotIn(("lock", False), harness.timeline[:apply_index])
        self.assertEqual(harness.journal.document["written_lock"], False)
        self.assertEqual(harness.journal.document["written_transform"], 0)

    def test_tablet_takes_lock_when_unlocked_before_rotating(self):
        harness = Harness(transform=2, locked=False)
        harness.display.apply(Target(mode="tablet", tablet=None, rotation=None, disable_internal_input=True))
        timeline = harness.timeline
        self.assertLess(timeline.index(("lock", True)), timeline.index("ApplyMonitorsConfig"))
        self.assertLess(timeline.index("ApplyMonitorsConfig"), timeline.index(("lock", False)))
        self.assertFalse(harness.settings.value)

    def test_laptop_after_tablet_keeps_ownership_until_unavailable_restores(self):
        harness = Harness(transform=0, locked=False)
        harness.display.apply(Target(mode="tablet", tablet=None, rotation=None, disable_internal_input=True))
        harness.mutter.transform = 1
        harness.display.apply(Target(mode="laptop", tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(harness.mutter.transform, 0)
        self.assertTrue(harness.settings.value)
        self.assertIsNotNone(harness.display.owned)
        self.assertEqual(harness.journal.document["written_transform"], 0)
        timeline = harness.timeline
        apply_index = len(timeline) - 1 - timeline[::-1].index("ApplyMonitorsConfig")
        lock_index = len(timeline) - 1 - timeline[::-1].index(("lock", True))
        self.assertLess(lock_index, apply_index)
        harness.display.apply(Target(mode="hardware"))
        self.assertEqual(harness.mutter.transform, 0)
        self.assertFalse(harness.settings.value)
        self.assertIsNotNone(harness.display.owned)
        self.assertIsNotNone(harness.journal.document)
        harness.display.apply(Target(mode="unavailable"))
        self.assertFalse(harness.settings.value)
        self.assertIsNone(harness.display.owned)
        self.assertIsNone(harness.journal.document)

    def test_initial_laptop_owns_landscape_until_hardware_then_restores(self):
        harness = Harness(transform=2, locked=True)
        harness.display.apply(Target(mode="laptop", tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(harness.mutter.transform, 0)
        self.assertTrue(harness.settings.value)
        self.assertIsNotNone(harness.journal.document)
        harness.display.apply(Target(mode="hardware"))
        self.assertEqual(harness.mutter.transform, 2)
        self.assertFalse(harness.settings.value)
        self.assertIsNotNone(harness.journal.document)
        harness.display.apply(Target(mode="unavailable"))
        self.assertTrue(harness.settings.value)
        self.assertIsNone(harness.journal.document)

    def test_tent_to_laptop_to_hardware_restores_original_state(self):
        harness = Harness(transform=2, locked=False)
        harness.display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        harness.display.apply(Target(mode="laptop", tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(harness.mutter.transform, 0)
        self.assertTrue(harness.settings.value)
        harness.display.apply(Target(mode="hardware"))
        self.assertEqual(harness.mutter.transform, 2)
        self.assertFalse(harness.settings.value)
        self.assertIsNotNone(harness.journal.document)
        harness.display.apply(Target(mode="unavailable"))
        self.assertIsNone(harness.journal.document)

    def test_hardware_mode_restores_display(self):
        harness = Harness(transform=0, locked=False)
        harness.display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertIsNotNone(harness.display.owned)
        harness.display.apply(Target(mode="hardware"))
        self.assertEqual(harness.mutter.transform, 0)
        self.assertFalse(harness.settings.value)
        self.assertIsNotNone(harness.display.owned)
        self.assertIsNotNone(harness.journal.document)

    def test_hardware_tablet_unlocks_original_orientation_lock(self):
        harness = Harness(transform=0, locked=True)
        harness.display.apply(Target(mode="hardware"))
        self.assertFalse(harness.settings.value)
        self.assertIsNotNone(harness.display.owned)
        self.assertEqual(harness.journal.document["original_lock"], True)

        harness.display.apply(Target(mode="unavailable"))
        self.assertTrue(harness.settings.value)
        self.assertIsNone(harness.display.owned)
        self.assertIsNone(harness.journal.document)

    def test_other_software_mode_restarts_transaction_when_output_changes(self):
        harness = Harness(transform=0, locked=False)
        harness.display.apply(
            Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True)
        )
        harness.mutter.monitor_spec = ("eDP-2", "AUO", "panel", "2")
        harness.mutter.transform = 0

        harness.display.apply(
            Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True)
        )

        self.assertEqual(harness.mutter.transform, 2)
        self.assertEqual(
            harness.journal.document["spec"], ["eDP-2", "AUO", "panel", "2"]
        )

    def test_unknown_mode_raises_stable_code(self):
        harness = Harness()
        with self.assertRaises(DisplayError) as caught:
            harness.display.apply(Target(mode="bogus"))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNKNOWN_MODE)
        self.assertEqual(harness.timeline, [])

    def test_unknown_dict_mode_raises(self):
        harness = Harness()
        with self.assertRaises(DisplayError) as caught:
            harness.display.apply({"mode": "sideways"})
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNKNOWN_MODE)


class FailureAndRecoveryTests(unittest.TestCase):
    def test_lock_write_failure_rolls_back(self):
        harness = Harness(transform=0, locked=False)
        harness.settings.set_result = False
        with self.assertRaises(DisplayError) as caught:
            harness.display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_LOCK)
        self.assertEqual(harness.mutter.apply_calls, 0)
        # Nothing was changed for real, so the rollback may clear the journal.
        self.assertFalse(harness.settings.value)

    def test_rotation_failure_rolls_back_lock(self):
        harness = Harness(transform=0, locked=False, fail_apply=10)
        with self.assertRaises(DisplayError) as caught:
            harness.display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        self.assertFalse(harness.settings.value)
        self.assertIsNone(harness.display.owned)

    def test_rotate_retries_apply_once(self):
        harness = Harness(transform=0, locked=False, fail_apply=1)
        harness.display.rotate(2, ("eDP-1", "AUO", "panel", "1"))
        self.assertEqual(harness.mutter.apply_calls, 2)
        self.assertEqual(harness.mutter.transform, 2)

    def test_readback_mismatch_raises_transform_error(self):
        harness = Harness(transform=0, locked=False, apply_updates=False)
        with self.assertRaises(DisplayError) as caught:
            harness.display.rotate(2, ("eDP-1", "AUO", "panel", "1"))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        self.assertEqual(harness.mutter.apply_calls, 2)
        self.assertTrue(harness.sleeps)  # delayed read-back retry

    def test_restore_keeps_journal_when_lock_writeback_fails(self):
        document = owned_journal(transform=0, original_transform=0, original_lock=True)
        # The fake write is accepted but never becomes visible: read-back fails,
        # so even the constructor recovery must keep the journal.
        harness = Harness(
            transform=0,
            locked=False,
            document=document,
            settings_kwargs={"readback_override": False},
        )
        self.assertIsNotNone(harness.display.owned)
        with self.assertRaises(DisplayError) as caught:
            harness.display.restore()
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_LOCK)
        self.assertIsNotNone(harness.journal.document)
        self.assertIsNotNone(harness.display.owned)

    def test_restore_keeps_journal_when_mutter_is_unavailable(self):
        document = owned_journal(transform=2, original_transform=0, original_lock=True)
        harness = Harness(
            transform=2, locked=True, document=document, mutter_fail_state=True
        )
        self.assertIsNotNone(harness.display.owned)
        with self.assertRaises(DisplayError) as caught:
            harness.display.restore()
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)
        self.assertIsNotNone(harness.journal.document)

    def test_startup_recovery_restores_journal(self):
        document = owned_journal(transform=2, original_transform=0, original_lock=True)
        harness = Harness(transform=2, locked=True, document=document)
        # The constructor already recovered the journal.
        self.assertEqual(harness.mutter.transform, 0)
        self.assertTrue(harness.settings.value)
        self.assertIsNone(harness.journal.document)
        self.assertIsNone(harness.display.owned)

    def test_startup_recovery_keeps_journal_when_display_not_ready(self):
        document = owned_journal(transform=2, original_transform=0, original_lock=True)
        timeline = []
        mutter = FakeMutter(timeline, transform=2)
        mutter.call_sync = lambda *args: (_ for _ in ()).throw(dbus_error("org.freedesktop.DBus.Error.NoReply"))
        settings = FakeSettings(timeline, locked=True)
        journal = FakeJournal(timeline, document)
        display = Display(mutter, settings, journal, lambda text: text, sleep=lambda _s: None)
        self.assertIsNotNone(display.owned)
        self.assertIsNotNone(journal.document)


if __name__ == "__main__":
    unittest.main()
