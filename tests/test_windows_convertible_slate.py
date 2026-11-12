"""Verified convertible-slate access and ETW-gated posture notification."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from panasonic_posture.model import HardwareStatus, Target
from panasonic_posture.contracts import ErrorCode
from panasonic_posture.session.failures import DisplayError, ServiceError
from panasonic_posture.windows.convertible_slate import (
    NativeSlateDevice,
    SlateError,
    SlateNotifier,
)
from panasonic_posture.windows.session_display import QualifiedDisplay, SessionHardwareQuery
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
            raise DisplayError('display-transform-rejected')

    def hold_orientation(self):
        self.calls.append(('hold', None))

    def restore(self, normal=False):
        self.calls.append(('restore', normal))


class StatefulDisplay(FakeDisplay):
    """Model a Windows display changed by the platform after a CSM write."""

    def __init__(self):
        super().__init__()
        self.mode = None
        self.locked = False
        self.corrupted = False
        self.sticky_corruption = False

    def apply(self, target):
        super().apply(target)
        self.mode = target.mode
        self.locked = True
        if not self.sticky_corruption:
            self.corrupted = False

    def verify_software_target(self, target):
        if self.corrupted:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'rotation-lock-took-over')
        if not self.locked or self.mode != target.mode:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'fixed-rotation-changed')


def healthy(value=False):
    return lambda: (value, HardwareStatus.OK)


class SlateNotifierTests(unittest.TestCase):
    def test_postures_call_an_idempotent_set_and_track_the_temporary_lease(self):
        device = FakeSlateDevice(False)
        notifier = SlateNotifier(device, hardware_query=healthy())
        self.assertTrue(notifier.sync_mode('tent'))
        self.assertFalse(notifier.sync_mode('stand'))
        self.assertFalse(notifier.sync_mode('hardware'))
        self.assertTrue(notifier.sync_mode('laptop'))
        self.assertFalse(notifier.sync_mode('unavailable'))
        self.assertEqual(device.sets, [True, True, True, False])
        self.assertFalse(notifier.original)
        self.assertFalse(notifier.last)

    def test_every_write_and_clear_is_blocked_until_the_physical_source_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            notifier = SlateNotifier(device, journal, hardware_query=lambda: (None, HardwareStatus.ABSENT))
            for action in (lambda: notifier.sync_mode('laptop'), notifier.release):
                with self.assertRaises(SlateError):
                    action()
            self.assertEqual(device.sets, [])
            self.assertIsNotNone(journal.load())
            self.assertIsNotNone(notifier.pending)
            notifier.closed = True

    def test_crash_recovery_restores_the_journal_original_before_a_new_target(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            notifier = SlateNotifier(device, journal, hardware_query=healthy())
            self.assertTrue(notifier.sync_mode('tent'))
            self.assertEqual(device.sets, [False, True])
            self.assertFalse(notifier.original)
            self.assertTrue(notifier.last)
            notifier.close()
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())

    def test_recovery_for_laptop_restores_and_clears_without_an_extra_flash(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            notifier = SlateNotifier(device, journal, hardware_query=healthy())
            self.assertFalse(notifier.sync_mode('laptop'))
            self.assertEqual(device.sets, [False, False])
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())
            self.assertIsNone(notifier.pending)

    def test_failed_set_keeps_the_journal_and_never_advances_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            device = FakeSlateDevice(False)
            device.fail_set = True
            notifier = SlateNotifier(device, journal, hardware_query=healthy())
            with self.assertRaises(SlateError):
                notifier.sync_mode('tent')
            self.assertFalse(notifier.original)
            self.assertFalse(notifier.last)
            self.assertIsNotNone(journal.load())
            device.fail_set = False
            notifier.close()
            self.assertIsNone(journal.load())

    def test_an_unchanged_set_result_does_not_adopt_a_new_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            device = FakeSlateDevice(False)
            notifier = SlateNotifier(device, journal, hardware_query=healthy())
            device.state = True  # External state already matches the next target.
            self.assertFalse(notifier.sync_mode('tent'))
            self.assertFalse(notifier.original)
            self.assertTrue(notifier.last)
            self.assertEqual(journal.load().original, False)
            notifier.release()
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())

    def test_release_restores_startup_state_and_keeps_the_device_open(self):
        device = FakeSlateDevice(True)
        notifier = SlateNotifier(device, hardware_query=healthy(True))
        notifier.sync_mode('tablet')
        notifier.release()
        self.assertTrue(device.state)
        self.assertFalse(device.closed)
        notifier.close()
        self.assertTrue(device.closed)

    def test_failed_release_keeps_the_journal_for_the_next_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            device = FakeSlateDevice(False)
            notifier = SlateNotifier(device, journal, hardware_query=healthy())
            notifier.sync_mode('tent')
            device.fail_set = True
            with self.assertRaises(SlateError):
                notifier.release()
            self.assertIsNotNone(journal.load())
            self.assertTrue(notifier.last)
            device.fail_set = False
            notifier.close()
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())

    def test_session_query_accepts_only_a_known_healthy_boolean(self):
        from panasonic_posture.model import StateSnapshot
        query = SessionHardwareQuery(lambda: StateSnapshot(hardware=True, hardware_status=HardwareStatus.OK))
        self.assertEqual(query(), (True, HardwareStatus.OK))
        for state in (StateSnapshot(hardware=None, hardware_status=HardwareStatus.ABSENT),
                      StateSnapshot(hardware=None, hardware_status=HardwareStatus.READ_ERROR),
                      StateSnapshot(hardware=1, hardware_status=HardwareStatus.OK)):
            self.assertEqual(SessionHardwareQuery(lambda state=state: state)(),
                             (None, HardwareStatus.READ_ERROR if state.hardware_status == HardwareStatus.OK
                              else state.hardware_status))
        def broken():
            raise OSError('service unavailable')
        self.assertEqual(SessionHardwareQuery(broken)(), (None, HardwareStatus.READ_ERROR))
        def rejected():
            raise ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, 'session context unavailable')
        self.assertEqual(SessionHardwareQuery(rejected)(), (None, HardwareStatus.READ_ERROR))

    def test_journal_path_is_a_sibling_of_the_display_journal(self):
        self.assertEqual(
            lease_path(Path('state/native-display.json')),
            Path('state/native-display-slate.json'),
        )


class NativeSlateDeviceTests(unittest.TestCase):
    def test_device_toggle_is_conditional_and_read_back(self):
        device = object.__new__(NativeSlateDevice)
        state = [1]
        writes = []
        device.handle = 1
        device._raw_read = lambda: state[0]
        device.sleep = lambda _: None

        def write(handle, data, size, count, overlapped):
            del handle, data, size, overlapped
            writes.append(True)
            state[0] = 0
            count._obj.value = 1
            return 1

        device.WriteFile = write
        self.assertTrue(device.set(True))
        self.assertEqual(writes, [True])
        self.assertFalse(device.set(True))
        self.assertEqual(writes, [True])

    def test_verified_raw_bytes_map_zero_to_slate_one_to_laptop(self):
        device = object.__new__(NativeSlateDevice)
        device._raw_read = lambda: 0
        self.assertTrue(device.read())
        device._raw_read = lambda: 1
        self.assertFalse(device.read())

    def test_toggle_refuses_more_than_one_write_and_surfaces_readback_mismatch(self):
        device = object.__new__(NativeSlateDevice)
        writes = []
        sleeps = []
        device.handle = 1
        device._raw_read = lambda: 1
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


class QualifiedDisplayTests(unittest.TestCase):
    def test_software_display_apply_precedes_slate_notification(self):
        events = []
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: events.append(('notify', mode))
        display, context = FakeDisplay(), FakeContext()
        original_apply = display.apply

        def apply(target):
            events.append(('apply', target.mode))
            original_apply(target)

        display.apply = apply
        QualifiedDisplay(display, context, slate).apply(Target(mode='tent'))
        self.assertEqual(events, [('apply', 'tent'), ('notify', 'tent')])

    def test_platform_change_after_slate_notification_reasserts_the_fixed_target(self):
        events = []
        display = StatefulDisplay()
        original_apply = display.apply

        def apply(target):
            events.append(('apply', target.mode))
            original_apply(target)

        display.apply = apply
        slate = mock.Mock()

        def notify(mode):
            events.append(('notify', mode))
            # Windows can finish its own tablet response even though the CCD
            # transaction had already engaged the lock.
            display.corrupted = True

        slate.sync_mode.side_effect = notify
        QualifiedDisplay(display, FakeContext(), slate).apply(Target(mode='tent'))
        self.assertEqual(events, [
            ('apply', 'tent'),
            ('notify', 'tent'),
            ('apply', 'tent'),
        ])
        self.assertEqual(display.mode, 'tent')
        self.assertTrue(display.locked)

    def test_failed_post_slate_reassertion_restores_without_a_ready_receipt(self):
        display = StatefulDisplay()
        display.sticky_corruption = True
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: setattr(display, 'corrupted', True)
        proxy = QualifiedDisplay(display, FakeContext(), slate)
        with self.assertRaises(DisplayError):
            proxy.apply(Target(mode='tent'))
        self.assertEqual(display.calls, [
            ('apply', 'tent'),
            ('apply', 'tent'),
            ('restore', False),
        ])
        slate.release.assert_called_once_with()

    def test_hardware_handoff_keeps_slate_notification_first(self):
        events = []
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: events.append(('notify', mode))
        display, context = FakeDisplay(), FakeContext()
        original_apply = display.apply

        def apply(target):
            events.append(('apply', target.mode))
            original_apply(target)

        display.apply = apply
        QualifiedDisplay(display, context, slate).apply(Target(mode='hardware'))
        self.assertEqual(events, [('notify', 'hardware'), ('apply', 'hardware')])

    def test_notification_failure_restores_display_and_releases_slate(self):
        slate = mock.Mock()
        slate.sync_mode.side_effect = SlateError('device unavailable')
        display, context = FakeDisplay(), FakeContext()
        proxy = QualifiedDisplay(display, context, slate)
        with self.assertRaises(DisplayError) as caught:
            proxy.apply(Target(mode='tent'))
        self.assertEqual(caught.exception.detail, 'convertible-slate-notification-failed')
        self.assertEqual(
            display.calls,
            [('apply', 'tent'), ('restore', False)],
        )
        slate.release.assert_called_once_with()

    def test_display_failure_releases_slate_without_faking_a_receipt(self):
        slate = mock.Mock()
        display, context = FakeDisplay(), FakeContext()
        display.fail = True
        proxy = QualifiedDisplay(display, context, slate)
        with self.assertRaises(DisplayError):
            proxy.apply(Target(mode='tent'))
        self.assertEqual(display.calls, [('apply', 'tent'), ('restore', False)])
        slate.sync_mode.assert_not_called()
        slate.release.assert_called_once_with()

    def test_restore_releases_slate_even_if_display_restore_fails(self):
        class Display(FakeDisplay):
            def restore(self, normal=False):
                super().restore(normal)
                raise DisplayError('display-identity-changed')

        slate = mock.Mock()
        with self.assertRaises(DisplayError):
            QualifiedDisplay(Display(), FakeContext(), slate).restore()
        slate.release.assert_called_once_with()


if __name__ == '__main__':
    unittest.main()
