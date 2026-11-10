"""Verified convertible-slate access and independent posture notification."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from panasonic_posture.model import HardwareStatus, Target
from panasonic_posture.session.failures import DisplayError
from panasonic_posture.windows.convertible_slate import (
    NativeSlateDevice,
    SlateError,
    SlateNotifier,
)
from panasonic_posture.windows.session_display import QualifiedDisplay, slate_qualified_hardware
from panasonic_posture.windows.slate_journal import SlateJournal, lease_path


class FakeSlateDevice:
    def __init__(self, state=False):
        self.state = state
        self.sets = []
        self.reads = 0
        self.closed = False
        self.fail_set = False

    def read(self):
        self.reads += 1
        return self.state

    def set(self, slate):
        self.sets.append(slate)
        if self.fail_set:
            raise SlateError('write failed')
        changed = self.state != slate
        self.state = slate
        return changed

    def close(self):
        self.closed = True


class FakeContext:
    def __init__(self):
        self.available = True
        self.closed = False

    def control_available(self):
        return self.available

    def poll(self):
        pass

    def close(self):
        self.closed = True


class FakeDisplay:
    def __init__(self):
        self.calls = []
        self.fail = False

    def apply(self, target):
        self.calls.append(('apply', target.mode))
        if self.fail:
            raise DisplayError('display' + '-transform-rejected')

    def hold_orientation(self):
        self.calls.append(('hold', None))

    def restore(self, normal=False):
        self.calls.append(('restore', normal))


class SlateNotifierTests(unittest.TestCase):
    def test_postures_set_only_when_the_desired_slate_state_changes(self):
        now = [10.0]
        device = FakeSlateDevice(False)
        notifier = SlateNotifier(device, clock=lambda: now[0])
        self.assertTrue(notifier.sync_mode('tent'))
        self.assertFalse(notifier.sync_mode('stand'))
        self.assertFalse(notifier.sync_mode('hardware'))
        self.assertTrue(notifier.sync_mode('laptop'))
        self.assertFalse(notifier.sync_mode('unavailable'))
        self.assertIs(notifier.software_owned, False)
        now[0] += 2.1
        self.assertIsNone(notifier.software_owned)
        self.assertEqual(device.sets, [True, False])

    def test_notifier_exposes_only_the_slate_value_it_changed(self):
        now = [10.0]
        notifier = SlateNotifier(FakeSlateDevice(False), clock=lambda: now[0])
        self.assertIsNone(notifier.software_owned)
        notifier.sync_mode('tent')
        self.assertIs(notifier.software_owned, True)
        notifier.sync_mode('laptop')
        self.assertIs(notifier.software_owned, False)
        now[0] += 2.1
        self.assertIsNone(notifier.software_owned)

    def test_external_change_during_software_ownership_releases_the_lease(self):
        now = [10.0]
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            device = FakeSlateDevice(False)
            notifier = SlateNotifier(device, journal, clock=lambda: now[0])
            self.assertTrue(notifier.sync_mode('stand'))
            self.assertTrue(journal.path.exists())
            now[0] += 2.1
            self.assertIs(notifier.software_owned, True)

            # Moving out of tent/stand changes the physical indicator while the
            # app still owns the previous slate value.
            device.state = False
            self.assertIsNone(notifier.software_owned)
            self.assertIs(notifier.original, False)
            self.assertIs(notifier.last, False)
            self.assertIsNone(journal.load())

    def test_external_physical_target_is_adopted_without_software_ownership(self):
        device = FakeSlateDevice(True)
        notifier = SlateNotifier(device)
        self.assertFalse(notifier.sync_mode('hardware'))
        self.assertTrue(notifier.original)
        self.assertTrue(notifier.last)
        self.assertIsNone(notifier.software_owned)

    def test_slate_aware_hardware_masks_only_the_owned_indicator_value(self):
        owned = [True]
        scoped = slate_qualified_hardware(
            lambda: (True, HardwareStatus.OK), lambda: owned[0]
        )
        self.assertEqual(scoped(), (False, HardwareStatus.OK))
        owned[0] = None
        self.assertEqual(scoped(), (True, HardwareStatus.OK))
        owned[0] = False
        self.assertEqual(scoped(), (None, HardwareStatus.AMBIGUOUS))
        error = slate_qualified_hardware(
            lambda: (None, HardwareStatus.READ_ERROR), lambda: True
        )
        self.assertEqual(error(), (None, HardwareStatus.READ_ERROR))
        settling = slate_qualified_hardware(
            lambda: (False, HardwareStatus.OK), lambda: True,
            lambda: True,
        )
        self.assertEqual(settling(), (False, HardwareStatus.OK))

    def test_release_restores_startup_state_and_keeps_the_lease_reusable(self):
        device = FakeSlateDevice(True)
        notifier = SlateNotifier(device)
        notifier.sync_mode('tablet')
        notifier.release()
        self.assertTrue(device.state)
        self.assertFalse(device.closed)
        notifier.sync_mode('stand')
        self.assertTrue(device.state)
        notifier.close()
        self.assertTrue(device.closed)
        self.assertTrue(device.state)

    def test_journal_restart_in_the_target_posture_adopts_it_without_a_toggle(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            device = FakeSlateDevice(False)
            first = SlateNotifier(device, journal)
            self.assertTrue(first.sync_mode('tent'))
            self.assertTrue(device.state)
            self.assertTrue(journal.path.exists())

            second = SlateNotifier(device, journal)
            self.assertTrue(device.state)
            self.assertEqual(device.sets, [True])
            self.assertTrue(journal.path.exists())
            self.assertFalse(second.sync_mode('tent'))
            self.assertTrue(device.state)
            self.assertEqual(device.sets, [True])
            self.assertIsNone(journal.load())
            self.assertTrue(second.original)
            second.close()
            self.assertTrue(device.state)

    def test_deferred_recovery_returns_to_the_baseline_for_a_different_posture(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            device = FakeSlateDevice(False)
            first = SlateNotifier(device, journal)
            self.assertTrue(first.sync_mode('tent'))
            self.assertTrue(journal.path.exists())

            second = SlateNotifier(device, journal)
            self.assertEqual(device.sets, [True])
            self.assertTrue(second.sync_mode('laptop'))
            self.assertFalse(device.state)
            self.assertEqual(device.sets, [True, False])
            self.assertIsNone(journal.load())
            self.assertFalse(second.original)
            second.close()
            self.assertFalse(device.state)

    def test_deferred_recovery_into_a_new_target_retains_the_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(False)
            notifier = SlateNotifier(device, journal)
            self.assertTrue(notifier.sync_mode('tent'))
            self.assertTrue(device.state)
            self.assertIsNotNone(journal.load())
            self.assertIsNone(notifier.pending)
            self.assertFalse(notifier.sync_mode('tent'))
            self.assertIsNotNone(journal.load())
            notifier.close()
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())

    def test_failed_deferred_recovery_retains_the_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            device.fail_set = True
            notifier = SlateNotifier(device, journal)
            with self.assertRaises(SlateError):
                notifier.sync_mode('laptop')
            self.assertTrue(journal.path.exists())
            device.fail_set = False
            notifier.close()
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())
            self.assertTrue(device.closed)

    def test_journal_path_is_a_sibling_of_the_display_journal(self):
        self.assertEqual(
            lease_path(Path('state/native-display.json')),
            Path('state/native-display-slate.json'),
        )

    def test_device_toggle_is_conditional_and_read_back(self):
        device = object.__new__(NativeSlateDevice)
        state = [0]
        writes = []
        sleeps = []
        device.handle = 1
        device._raw_read = lambda: state[0]
        device.sleep = sleeps.append

        def write(handle, data, size, count, overlapped):
            del handle, data, size, overlapped
            writes.append(True)
            state[0] = 1
            count._obj.value = 1
            return 1

        device.WriteFile = write
        self.assertTrue(device.set(True))
        self.assertEqual(writes, [True])
        self.assertFalse(device.set(True))
        self.assertEqual(writes, [True])

    def test_verified_raw_bytes_map_zero_to_laptop_one_to_slate(self):
        device = object.__new__(NativeSlateDevice)
        device._raw_read = lambda: 0
        self.assertFalse(device.read())
        device._raw_read = lambda: 1
        self.assertTrue(device.read())

    def test_toggle_refuses_more_than_one_write_and_surfaces_readback_mismatch(self):
        device = object.__new__(NativeSlateDevice)
        writes = []
        sleeps = []
        device.handle = 1
        device._raw_read = lambda: 0
        device.sleep = sleeps.append

        def write(handle, data, size, count, overlapped):
            del handle, data, size, overlapped
            writes.append(True)
            count._obj.value = 1
            return 1

        device.WriteFile = write
        with self.assertRaises(SlateError):
            device.set(True)
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(sleeps), 10)

    def test_orientation_is_held_before_notification_and_apply(self):
        events = []
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: events.append(('notify', mode))
        display, context = FakeDisplay(), FakeContext()
        original_hold = display.hold_orientation
        original_apply = display.apply

        def hold():
            events.append(('hold', None))
            original_hold()

        def apply(target):
            events.append(('apply', target.mode))
            original_apply(target)

        display.hold_orientation, display.apply = hold, apply
        QualifiedDisplay(display, context, slate).apply(Target(mode='tent'))
        self.assertEqual(events, [
            ('hold', None), ('notify', 'tent'), ('apply', 'tent'),
        ])

    def test_notification_failure_restores_hold_and_releases_slate(self):
        slate = mock.Mock()
        slate.sync_mode.side_effect = SlateError('device unavailable')
        display, context = FakeDisplay(), FakeContext()
        proxy = QualifiedDisplay(display, context, slate)
        with self.assertRaises(DisplayError) as caught:
            proxy.apply(Target(mode='tent'))
        self.assertEqual(
            caught.exception.detail, 'convertible-slate-notification-failed'
        )
        self.assertEqual(display.calls, [('hold', None), ('restore', False)])
        slate.release.assert_called_once_with()

    def test_display_failure_releases_slate_without_faking_a_receipt(self):
        slate = mock.Mock()
        display, context = FakeDisplay(), FakeContext()
        display.fail = True
        proxy = QualifiedDisplay(display, context, slate)
        with self.assertRaises(DisplayError):
            proxy.apply(Target(mode='tent'))
        self.assertEqual(display.calls, [
            ('hold', None), ('apply', 'tent'), ('restore', False),
        ])
        slate.sync_mode.assert_called_once_with('tent')
        slate.release.assert_called_once_with()

    def test_successful_hold_does_not_release_the_notification(self):
        slate = mock.Mock()
        display, context = FakeDisplay(), FakeContext()
        QualifiedDisplay(display, context, slate).hold_orientation()
        self.assertEqual(display.calls, [('hold', None)])
        slate.release.assert_not_called()

    def test_restore_releases_slate_even_if_display_restore_fails(self):
        class Display(FakeDisplay):
            def restore(self, normal=False):
                super().restore(normal)
                raise DisplayError('display' + '-identity-changed')

        slate = mock.Mock()
        proxy = QualifiedDisplay(Display(), FakeContext(), slate)
        with self.assertRaises(DisplayError):
            proxy.restore()
        slate.release.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
