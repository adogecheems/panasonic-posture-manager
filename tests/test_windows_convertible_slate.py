"""Verified convertible-slate access and ETW-gated posture notification."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from panasonic_posture.model import HardwareStatus, StateSnapshot, Target
from panasonic_posture.contracts import ErrorCode
from panasonic_posture.session.failures import DisplayError, FailureKind, ServiceError
from panasonic_posture.windows.hardware.convertible_slate import (
    NativeSlateDevice,
    SlateError,
    SlateDeviceAbsent,
    SlateNotifier,
    slate_instance_matches,
)
from panasonic_posture.windows.session.display import QualifiedDisplay, SessionHardwareQuery
from panasonic_posture.windows.session.qualification import QualifiedSessionClient
from panasonic_posture.windows.hardware.slate_journal import SlateJournal, lease_path


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
    def test_corrupt_record_is_not_isolated_before_the_physical_gate_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'slate.json'
            path.write_bytes(b'{')
            device = FakeSlateDevice(False)
            status = [HardwareStatus.ABSENT]
            notifier = SlateNotifier(device, SlateJournal(path),
                                     hardware_query=lambda: (False, status[0]))
            self.addCleanup(notifier.close)
            self.assertTrue(path.exists())
            notifier.release()  # An idempotent release must also remain read-only.
            with self.assertRaises(SlateError):
                notifier.sync_mode('tent')
            self.assertEqual(path.read_bytes(), b'{')
            self.assertEqual(device.sets, [])
            status[0] = HardwareStatus.OK
            notifier.sync_mode('tent')
            isolated = list(path.parent.glob('slate.json.corrupt-*'))
            self.assertEqual(len(isolated), 1)
            self.assertEqual(isolated[0].read_bytes(), b'{')
            self.assertTrue(device.state)

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

    def test_crash_recovery_in_the_same_target_retains_the_lease_until_release(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            notifier = SlateNotifier(device, journal, hardware_query=healthy())
            self.assertFalse(notifier.sync_mode('tent'))
            self.assertEqual(device.sets, [True])
            self.assertFalse(notifier.original)
            self.assertTrue(notifier.last)
            self.assertIsNotNone(journal.load())
            notifier.close()
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())

    def test_software_posture_to_hardware_reuses_the_lease_without_a_laptop_flash(self):
        for mode in ('tent', 'stand'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                journal = SlateJournal(Path(directory) / 'slate.json')
                journal.save(False, True)
                device = FakeSlateDevice(True)
                notifier = SlateNotifier(device, journal, hardware_query=healthy(True))
                self.assertFalse(notifier.sync_mode(mode))
                self.assertFalse(notifier.sync_mode('hardware'))
                self.assertEqual(device.sets, [True, True])
                self.assertTrue(device.state)
                self.assertIsNotNone(journal.load())
                notifier.close()
                self.assertFalse(device.state)
                self.assertIsNone(journal.load())

    def test_new_lease_is_retained_in_memory_for_the_next_same_target(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            device = FakeSlateDevice(False)
            notifier = SlateNotifier(device, journal, hardware_query=healthy(True))
            self.assertTrue(notifier.sync_mode('tent'))
            self.assertIsNotNone(notifier.pending)
            self.assertFalse(notifier.sync_mode('hardware'))
            self.assertEqual(device.sets, [True, True])
            self.assertIsNotNone(journal.load())
            notifier.close()
            self.assertFalse(device.state)
            self.assertIsNone(journal.load())

    def test_physical_tablet_ignores_a_stale_laptop_notification(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            notifier = SlateNotifier(device, journal, hardware_query=healthy(True))
            self.assertFalse(notifier.sync_mode('tent'))
            self.assertFalse(notifier.sync_mode('laptop'))
            self.assertTrue(device.state)
            self.assertEqual(device.sets, [True])
            self.assertIsNotNone(journal.load())
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

    def test_failed_same_target_recovery_keeps_the_pending_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            device.fail_set = True
            notifier = SlateNotifier(device, journal, hardware_query=healthy(True))
            with self.assertRaises(SlateError):
                notifier.sync_mode('hardware')
            self.assertIsNotNone(notifier.pending)
            self.assertIsNotNone(journal.load())
            self.assertTrue(device.state)
            device.fail_set = False
            notifier.close()
            self.assertFalse(device.state)
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

    def test_repeated_release_is_idempotent_without_another_hardware_query(self):
        queries = []

        def hardware():
            queries.append(True)
            return True, HardwareStatus.OK

        device = FakeSlateDevice(False)
        notifier = SlateNotifier(device, hardware_query=hardware)
        notifier.sync_mode('tent')
        notifier.release()
        notifier.release()
        self.assertEqual(device.sets, [True, False])
        self.assertEqual(len(queries), 2)
        notifier.close()
        self.assertEqual(len(queries), 2)

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
        query = SessionHardwareQuery(lambda: StateSnapshot(hardware=True, hardware_status=HardwareStatus.OK))
        self.assertEqual(query(), (True, HardwareStatus.OK))
        for state in (StateSnapshot(hardware=None, hardware_status=HardwareStatus.ABSENT),
                      StateSnapshot(hardware=None, hardware_status=HardwareStatus.READ_ERROR),
                      StateSnapshot(hardware=1, hardware_status=HardwareStatus.OK)):
            self.assertEqual(SessionHardwareQuery(lambda state=state: state)(),
                             (None, HardwareStatus.READ_ERROR if state.hardware_status == HardwareStatus.OK
                              else state.hardware_status))
        broken_calls = []

        def broken():
            broken_calls.append(True)
            raise OSError('service unavailable')
        sleeps = []
        self.assertEqual(
            SessionHardwareQuery(broken, sleep=sleeps.append)(),
            (None, HardwareStatus.READ_ERROR),
        )
        self.assertEqual(len(broken_calls), 6)
        self.assertEqual(sleeps, [0.1] * 5)

        def rejected():
            raise ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, 'session context unavailable')
        self.assertEqual(SessionHardwareQuery(rejected)(), (None, HardwareStatus.READ_ERROR))

        timeout_calls = []
        timeout_sleeps = []

        def timed_out():
            timeout_calls.append(True)
            raise TimeoutError('hardware request timed out')

        self.assertEqual(
            SessionHardwareQuery(timed_out, sleep=timeout_sleeps.append)(),
            (None, HardwareStatus.READ_ERROR),
        )
        self.assertEqual(len(timeout_calls), 1)
        self.assertEqual(timeout_sleeps, [])

    def test_session_query_reconnects_after_a_transport_failure(self):
        calls = []
        sleeps = []
        released = StateSnapshot(hardware=False, hardware_status=HardwareStatus.OK)

        def load():
            calls.append(True)
            if len(calls) == 1:
                raise OSError(233, 'hardware pipe write failed')
            return released

        self.assertEqual(
            SessionHardwareQuery(load, sleep=sleeps.append)(),
            (False, HardwareStatus.OK),
        )
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [0.1])

        rejected_calls = []

        def rejected():
            rejected_calls.append(True)
            raise ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, 'session context unavailable')

        self.assertEqual(
            SessionHardwareQuery(rejected, sleep=lambda _delay: None)(),
            (None, HardwareStatus.READ_ERROR),
        )
        self.assertEqual(len(rejected_calls), 1)

    def test_journal_path_is_a_sibling_of_the_display_journal(self):
        self.assertEqual(
            lease_path(Path('state/native-display.json')),
            Path('state/native-display-slate.json'),
        )


class NativeSlateDeviceTests(unittest.TestCase):
    def test_discovery_distinguishes_absence_from_ambiguity(self):
        for instances, error in (((), SlateDeviceAbsent),
                                 ((r'ACPI\INT33D3\a', r'ACPI\INT33D3\b'), SlateError)):
            with self.subTest(instances=instances):
                device = object.__new__(NativeSlateDevice)
                device.SetupDiGetClassDevsW = mock.Mock(return_value=1)
                device.SetupDiEnumDeviceInfo = lambda handle, index, info: index < len(instances)
                device.SetupDiDestroyDeviceInfoList = mock.Mock()
                identities = iter(instances)

                def identity(handle, info, buffer, length, size):
                    buffer.value = next(identities)
                    return True

                device.SetupDiGetDeviceInstanceIdW = identity
                with mock.patch('ctypes.get_last_error', return_value=259, create=True):
                    with self.assertRaises(error) as caught:
                        device._physical_device_object_name(r'ACPI\INT33D3')
                if instances:
                    self.assertNotIsInstance(caught.exception, SlateDeviceAbsent)
                device.SetupDiDestroyDeviceInfoList.assert_called_once_with(1)

    def test_physical_reader_requests_only_generic_read(self):
        device = object.__new__(NativeSlateDevice)
        device.read_only = True
        device.CreateFileW = mock.Mock(return_value=123)
        self.assertEqual(device._open_pdo(r'\Device\PDO-test'), 123)
        self.assertEqual(device.CreateFileW.call_args.args[1], 0x80000000)

    def test_slate_capability_uses_the_unique_hardware_id_not_one_instance(self):
        for instance in (
            r"ACPI\INT33D3\2&DABA3FF&0",
            r"ACPI\INT33D3\2&OTHERFIRMWARE&0",
            r"acpi\int33d3\different",
        ):
            with self.subTest(instance=instance):
                self.assertTrue(slate_instance_matches(instance))
        for instance in (
            "",
            r"ACPI\INT33D30\2&DABA3FF&0",
            r"ACPI\INT33D4\2&DABA3FF&0",
            r"PCI\INT33D3\2&DABA3FF&0",
        ):
            with self.subTest(instance=instance):
                self.assertFalse(slate_instance_matches(instance))
        self.assertFalse(slate_instance_matches(r"ACPI\INT33D3\x", ""))

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
    def test_tent_publication_precedes_the_display_apply_and_settle(self):
        events = []
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: events.append(('notify', mode))
        display, context = FakeDisplay(), FakeContext()
        original_apply = display.apply

        def apply(target):
            events.append(('apply', target.mode))
            original_apply(target)

        display.apply = apply
        QualifiedDisplay(
            display,
            context,
            slate,
            settle=lambda delay: events.append(('settle', delay)),
        ).apply(Target(mode='tent'))
        self.assertEqual(events, [
            ('notify', 'tent'),
            ('settle', 0.5),
            ('apply', 'tent'),
        ])

    def test_laptop_display_apply_precedes_slate_notification(self):
        events = []
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: events.append(('notify', mode))
        display, context = FakeDisplay(), FakeContext()
        original_apply = display.apply

        def apply(target):
            events.append(('apply', target.mode))
            original_apply(target)

        display.apply = apply
        QualifiedDisplay(display, context, slate).apply(Target(mode='laptop'))
        self.assertEqual(events, [('apply', 'laptop'), ('notify', 'laptop')])

    def test_platform_change_after_slate_notification_reasserts_the_fixed_target(self):
        events = []
        display = StatefulDisplay()
        original_apply = display.apply
        applies = []

        def apply(target):
            events.append(('apply', target.mode))
            original_apply(target)
            applies.append(True)
            if len(applies) == 1:
                # Windows finishes its own tablet response after the final
                # write, taking the lock back one last time.
                display.corrupted = True

        display.apply = apply
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: events.append(('notify', mode))

        QualifiedDisplay(
            display,
            FakeContext(),
            slate,
            settle=lambda delay: events.append(('settle', delay)),
        ).apply(Target(mode='tent'))
        self.assertEqual(events, [
            ('notify', 'tent'),
            ('settle', 0.5),
            ('apply', 'tent'),
            ('apply', 'tent'),
        ])
        self.assertEqual(display.mode, 'tent')
        self.assertTrue(display.locked)

    def test_failed_post_slate_reassertion_keeps_the_csm_lease(self):
        display = StatefulDisplay()
        display.sticky_corruption = True
        slate = mock.Mock()
        slate.sync_mode.side_effect = lambda mode: setattr(display, 'corrupted', True)
        proxy = QualifiedDisplay(
            display,
            FakeContext(),
            slate,
            settle=lambda delay: None,
        )
        with self.assertRaises(DisplayError):
            proxy.apply(Target(mode='tent'))
        self.assertEqual(display.calls, [
            ('apply', 'tent'),
            ('apply', 'tent'),
            ('restore', False),
        ])
        slate.release.assert_not_called()

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

    def test_stale_software_target_does_not_republish_after_native_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            slate = SlateNotifier(device, journal, hardware_query=healthy(True))
            slate.sync_mode('tent')

            class HandoffDisplay(FakeDisplay):
                native_handoff_active = True

            display = HandoffDisplay()
            QualifiedDisplay(display, FakeContext(), slate).apply(Target(mode='laptop'))
            self.assertEqual(display.calls, [('apply', 'laptop')])
            self.assertTrue(device.state)
            self.assertEqual(device.sets, [True, True])
            self.assertIsNotNone(journal.load())
            slate.close()

    def test_physical_takeover_during_verification_keeps_the_tablet_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = SlateJournal(Path(directory) / 'slate.json')
            journal.save(False, True)
            device = FakeSlateDevice(True)
            slate = SlateNotifier(device, journal, hardware_query=healthy(True))
            slate.sync_mode('tent')

            class TakeoverDisplay(FakeDisplay):
                def verify_software_target(self, target):
                    raise DisplayError(
                        ErrorCode.DISPLAY_LOCK,
                        'physical-tablet-took-over',
                    )

            display = TakeoverDisplay()
            QualifiedDisplay(
                display,
                FakeContext(),
                slate,
                settle=lambda delay: None,
            ).apply(Target(mode='tent'))
            self.assertTrue(device.state)
            self.assertEqual(device.sets, [True, True, True])
            self.assertIsNotNone(journal.load())
            slate.close()

    def test_notification_failure_restores_display_and_releases_slate(self):
        slate = mock.Mock()
        slate.sync_mode.side_effect = SlateError('device unavailable')
        display, context = FakeDisplay(), FakeContext()
        proxy = QualifiedDisplay(
            display,
            context,
            slate,
            settle=lambda delay: None,
        )
        with self.assertRaises(DisplayError) as caught:
            proxy.apply(Target(mode='tent'))
        self.assertEqual(caught.exception.detail, 'convertible-slate-notification-failed')
        self.assertEqual(
            display.calls,
            [('restore', False)],
        )
        slate.release.assert_called_once_with()

    def test_display_failure_keeps_the_existing_slate_lease(self):
        slate = mock.Mock()
        display, context = FakeDisplay(), FakeContext()
        display.fail = True
        proxy = QualifiedDisplay(
            display,
            context,
            slate,
            settle=lambda delay: None,
        )
        with self.assertRaises(DisplayError):
            proxy.apply(Target(mode='tent'))
        self.assertEqual(display.calls, [('apply', 'tent'), ('restore', False)])
        slate.sync_mode.assert_called_once_with('tent')
        slate.release.assert_not_called()

    def test_context_loss_still_releases_the_slate_lease(self):
        slate = mock.Mock()
        display, context = FakeDisplay(), FakeContext()
        original_apply = display.apply

        def apply(target):
            original_apply(target)
            context.available = False

        display.apply = apply
        with self.assertRaises(DisplayError):
            QualifiedDisplay(
                display,
                context,
                slate,
                settle=lambda delay: None,
            ).apply(Target(mode='tent'))
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

    def test_transient_context_blip_fails_softly_without_restore_or_release(self):
        display, context = FakeDisplay(), FakeContext()
        context.available = False
        context.snapshot = lambda: dict(
            suspended=False, error=None, display_on=True, control_available=False,
            presence_stale=True,
            presence=dict(session_id=1, console_session_id=1, active=True, locked=False),
        )
        slate = mock.Mock()
        proxy = QualifiedDisplay(display, context, slate, settle=lambda delay: None)
        with self.assertRaises(DisplayError) as caught:
            proxy.apply(Target(mode='tent'))
        self.assertEqual(caught.exception.detail, 'native-session-control-transient')
        self.assertEqual(display.calls, [])
        slate.release.assert_not_called()

    def test_genuine_context_loss_still_restores_and_releases(self):
        display, context = FakeDisplay(), FakeContext()
        context.available = False
        context.snapshot = lambda: dict(
            suspended=False,
            presence=dict(active=True, locked=True),
        )
        slate = mock.Mock()
        proxy = QualifiedDisplay(display, context, slate, settle=lambda delay: None)
        with self.assertRaises(DisplayError) as caught:
            proxy.apply(Target(mode='tent'))
        self.assertEqual(caught.exception.detail, 'native session control is unavailable')
        # Entry qualification loss fails before any write; the runtime failure
        # mode owns the subsequent restore and CSM release.
        self.assertEqual(display.calls, [])
        slate.release.assert_not_called()


class QualifiedSessionClientTests(unittest.TestCase):
    def make_client(self, snapshot):
        context = FakeContext()
        context.available = False
        context.snapshot = snapshot
        state = StateSnapshot(
            instance='fixture',
            generation=1,
            session_connected=True,
        )
        inner = mock.Mock()
        inner.get_state.return_value = state
        return QualifiedSessionClient(inner, context), inner

    def test_stale_presence_blip_is_transient_and_does_not_report_lost(self):
        client, inner = self.make_client(
            lambda: dict(suspended=False, error=None, display_on=True, control_available=False,
                         presence_stale=True,
                         presence=dict(session_id=1, console_session_id=1, active=True, locked=False)),
        )
        with self.assertRaises(ServiceError) as caught:
            client.get_state()
        self.assertEqual(caught.exception.kind, FailureKind.UNAVAILABLE)
        inner.session_update.assert_not_called()

        with self.assertRaises(ServiceError) as caught:
            client.session_update(1, False)
        self.assertEqual(caught.exception.kind, FailureKind.UNAVAILABLE)
        inner.session_update.assert_not_called()

    def test_locked_console_is_still_an_immediate_rejection(self):
        client, inner = self.make_client(
            lambda: dict(suspended=False, presence=dict(active=True, locked=True)),
        )
        with self.assertRaises(ServiceError) as caught:
            client.get_state()
        self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        inner.session_update.assert_called_once_with(
            1,
            False,
            ErrorCode.DISPLAY_UNAVAILABLE,
            'native-session-unavailable',
        )


if __name__ == '__main__':
    unittest.main()
