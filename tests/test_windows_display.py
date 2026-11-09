"""Windows native panel selection and journalled rotation, without a desktop."""
from dataclasses import replace
import unittest

from panasonic_posture.windows.display import (
    Panel, select_panel, WindowsDisplay, NativeDisplay, DevMode, PathInfo, ModeInfo,
)
from panasonic_posture.session.failures import DisplayError
from panasonic_posture.windows.simulation import MemoryJournal
import ctypes as C
from types import SimpleNamespace
from panasonic_posture.model import HardwareStatus


def panel(**changes):
    return replace(Panel('panel-1', r'\\.\DISPLAY1', (1, 0, 0), 11, 0, 2880, 1920, 1), **changes)


class Api:
    def __init__(self):
        self.current = panel()
        self.writes = []
        self.fail = False

    def read(self):
        return self.current

    def rotate(self, identity, previous, transform):
        self.writes.append(transform)
        self.current = replace(self.current, transform=transform)
        if self.fail:
            self.fail = False
            raise DisplayError('display-transform-not-applied')


class WindowsDisplayTests(unittest.TestCase):
    def test_sdk_structures_have_fixed_windows_layout_on_every_platform(self):
        self.assertEqual(C.sizeof(DevMode), 220)
        self.assertEqual(C.sizeof(PathInfo), 72)
        self.assertEqual(C.sizeof(ModeInfo), 64)
        self.assertEqual(DevMode.orientation.offset, 84)

    def test_selects_only_unique_internal_unmirrored_panel(self):
        external = panel(identity='external', source=r'\\.\DISPLAY2', source_key=(1, 0, 1), technology=5)
        self.assertEqual(select_panel([external, panel()]), panel())
        for paths in ([], [external], [panel(), panel(identity='other')],
                      [panel(), replace(external, source_key=panel().source_key)],
                      [panel(identity='')], [panel(transform=8)]):
            with self.subTest(paths=paths), self.assertRaises(DisplayError):
                select_panel(paths)

    def test_tent_rotation_and_restore_reuse_the_ownership_journal(self):
        api, journal = Api(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(journal.document['written_transform'], 2)
        display.restore()
        self.assertEqual(api.current.transform, 0)
        self.assertIsNone(journal.document)

    def test_crash_and_failed_readback_keep_enough_information_to_restore(self):
        api, journal = Api(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        api.fail = True
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(api.current.transform, 0)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
        WindowsDisplay(api=api, journal=journal)
        self.assertEqual(api.current.transform, 0)
        self.assertIsNone(journal.document)

    def test_restore_preserves_external_changes_and_rejects_replacement_panels(self):
        api, journal = Api(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
        api.current = replace(api.current, transform=1)
        display.restore()
        self.assertEqual(api.current.transform, 1)
        self.assertEqual(api.writes, [2])
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
        api.current = replace(api.current, identity='replacement')
        with self.assertRaises(DisplayError):
            display.restore()
        self.assertIsNotNone(journal.document)
        self.assertEqual(api.writes, [2, 2])

    def test_does_not_claim_auto_rotation_control_or_override_an_unlocked_desktop(self):
        for mode, state in (('tent', 0), ('tablet', 1)):
            api, journal = Api(), MemoryJournal()
            api.current = panel(auto_rotation=state)
            display = WindowsDisplay(api=api, journal=journal)
            with self.subTest(mode=mode), self.assertRaises(DisplayError):
                display.apply(dict(mode=mode, tablet=True, rotation=None, disable_internal_input=True))
            self.assertFalse(api.writes)
            self.assertIsNone(journal.document)

    def test_physical_handoff_relinquishes_rotation_without_changing_os_policy(self):
        api, journal = Api(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        api.current = replace(api.current, auto_rotation=0)
        display.apply(dict(mode='hardware', tablet=True, rotation=None, disable_internal_input=False))
        self.assertIsNone(journal.document)
        self.assertIsNone(display.owned)
        api.current = replace(api.current, transform=1)
        display.restore()
        self.assertEqual(api.current.transform, 1)
        self.assertEqual(api.writes, [2])

    def test_restart_does_not_undo_native_rotation_after_the_os_takes_over(self):
        api, journal = Api(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        api.current = replace(api.current, auto_rotation=0)
        restarted = WindowsDisplay(api=api, journal=journal)
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(api.writes, [2])
        self.assertIsNone(journal.document)
        self.assertIsNone(restarted.owned)

    def test_restart_respects_physical_tablet_even_if_the_user_locked_native_rotation(self):
        api, journal = Api(), MemoryJournal()
        physical = [False]
        query = lambda: (physical[0], HardwareStatus.OK)
        display = WindowsDisplay(api=api, journal=journal, hardware_query=query)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        physical[0] = True
        WindowsDisplay(api=api, journal=journal, hardware_query=query)
        self.assertEqual(api.writes, [2])
        self.assertEqual(api.current.transform, 2)
        self.assertIsNone(journal.document)

    def test_physical_unknown_or_takeover_before_write_cannot_start_a_fixed_mode(self):
        for hardware, status in ((True, HardwareStatus.OK), (None, HardwareStatus.AMBIGUOUS)):
            api, journal = Api(), MemoryJournal()
            display = WindowsDisplay(api=api, journal=journal, hardware_query=lambda: (hardware, status))
            with self.assertRaises(DisplayError):
                display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
            self.assertEqual(api.writes, [])
            self.assertIsNone(journal.document)

    def test_failed_native_handoff_save_keeps_original_ownership_until_durable_retry(self):
        class FailedJournal(MemoryJournal):
            fail = False
            def save(self, document):
                if self.fail: raise OSError('full')
                super().save(document)
        api, journal = Api(), FailedJournal()
        display = WindowsDisplay(api=api, journal=journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        original = dict(journal.document)
        api.current = replace(api.current, auto_rotation=0)
        journal.fail = True
        with self.assertRaises(OSError): display.restore()
        self.assertEqual(journal.document, original)
        self.assertEqual(display.owned, original)
        journal.fail = False
        display.restore()
        self.assertEqual(api.writes, [2])
        self.assertIsNone(journal.document)

    def test_fixed_mode_is_not_confirmed_when_native_rotation_takes_over_without_a_write(self):
        class Race(Api):
            reads = 0
            def read(self):
                self.reads += 1
                if self.reads >= 3:
                    self.current = replace(self.current, auto_rotation=0)
                return self.current
        api, journal = Race(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='laptop', tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(api.writes, [])
        self.assertIsNone(journal.document)

    def test_late_native_takeover_does_not_restore_over_a_matching_native_direction(self):
        class Race(Api):
            def rotate(self, *args):
                super().rotate(*args)
                self.current = replace(self.current, auto_rotation=0)
        api, journal = Race(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        self.assertEqual(api.writes, [2])
        self.assertIsNone(journal.document)

    def test_post_hardware_hold_only_owns_rotation_when_os_auto_rotation_is_disabled(self):
        api, journal = Api(), MemoryJournal()
        display = WindowsDisplay(api=api, journal=journal)
        display.hold_orientation()
        self.assertEqual(api.writes, [])
        api.current = replace(api.current, transform=2)
        display.hold_orientation()
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.writes, [0])
        display.restore()
        self.assertIsNone(journal.document)
        api.current = replace(api.current, auto_rotation=0)
        with self.assertRaises(DisplayError): display.hold_orientation()
        self.assertIsNone(journal.document)


class NativeRotationTests(unittest.TestCase):
    def api(self, *, reject_test=False):
        api = object.__new__(NativeDisplay)
        state = [panel()]
        calls = []
        api.read = lambda: state[0]
        def mode(source):
            value = DevMode()
            value.size, value.orientation = C.sizeof(value), state[0].transform
            value.width, value.height = state[0].width, state[0].height
            return value
        api._mode = mode
        def write(source, raw_mode, window, flags, data):
            value = raw_mode._obj
            calls.append((source, value.orientation, value.width, value.height, value.fields, flags))
            if flags == 2:
                return -2 if reject_test else 0
            state[0] = replace(state[0], transform=value.orientation, width=value.width, height=value.height)
            return 0
        api.user = SimpleNamespace(ChangeDisplaySettingsExW=write)
        return api, state, calls

    def test_native_rotation_swaps_dimensions_and_never_persists_to_registry(self):
        api, state, calls = self.api()
        api.rotate('panel-1', 0, 1)
        self.assertEqual([call[5] for call in calls], [2, 0])
        self.assertTrue(all(call[1:5] == (1, 1920, 2880, 0x180080) for call in calls))
        self.assertEqual(state[0].transform, 1)

    def test_native_write_refuses_stale_identity_rotation_or_failed_mode_test(self):
        for identity, previous, transform, reject in (
                ('replacement', 0, 2, False), ('panel-1', 1, 2, False),
                ('panel-1', 0, True, False), ('panel-1', 0, 2, True)):
            api, state, calls = self.api(reject_test=reject)
            with self.subTest(identity=identity, previous=previous, reject=reject), self.assertRaises(DisplayError):
                api.rotate(identity, previous, transform)
            self.assertEqual(state[0].transform, 0)
            self.assertTrue(all(call[5] == 2 for call in calls))

    def test_native_write_refuses_an_active_os_rotator(self):
        api, state, calls = self.api()
        state[0] = replace(state[0], auto_rotation=0)
        with self.assertRaises(DisplayError): api.rotate('panel-1', 0, 2)
        self.assertEqual(calls, [])

    def test_native_query_retries_changing_buffer_sizes_only_within_bound(self):
        api = object.__new__(NativeDisplay)
        queries = []
        def sizes(flags, paths, modes):
            paths._obj.value, modes._obj.value = 1, 2
            return 0
        def query(*args):
            queries.append(True)
            return 122
        api.user = SimpleNamespace(GetDisplayConfigBufferSizes=sizes, QueryDisplayConfig=query)
        with self.assertRaises(DisplayError):
            api._paths()
        self.assertEqual(len(queries), 3)


if __name__ == '__main__':
    unittest.main()
