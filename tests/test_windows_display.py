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
        self.lock_writes = []
        self.shell = 0
        self.shell_writes = []
        self.events = []
        self.fail = False

    def read(self):
        return self.current

    def lock(self, locked):
        self.lock_writes.append(locked)
        self.events.append(('lock', locked))
        self.current = replace(self.current, auto_rotation=1 if locked else 0)

    def read_shell_mode(self):
        return self.shell

    def write_shell_mode(self, mode):
        self.shell_writes.append(mode)
        self.events.append(('shell', mode))
        self.shell = mode

    def rotate(self, identity, previous, transform):
        self.writes.append(transform)
        self.events.append(('rotate', transform))
        self.current = replace(self.current, transform=transform)
        if self.fail:
            self.fail = False
            raise DisplayError('display-transform-not-applied')


class WindowsDisplayTests(unittest.TestCase):
    def make(self, api=None, journal=None, hardware_query=None):
        return WindowsDisplay(
            api=api or Api(),
            journal=journal or MemoryJournal(),
            hardware_query=hardware_query,
        )

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

    def test_fixed_modes_lock_before_shell_and_rotation_then_restore_baseline(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)

        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        self.assertEqual(api.events, [('lock', True), ('shell', 1), ('rotate', 2)])
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(journal.document['original_lock'], False)
        self.assertEqual(journal.document['original_shell_mode'], 0)
        self.assertEqual(journal.document['written_lock'], True)
        self.assertEqual(journal.document['written_shell_mode'], 1)
        self.assertEqual(journal.document['written_transform'], 2)

        display.apply(dict(mode='stand', tablet=True, rotation=0, disable_internal_input=False))
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 1)

        display.apply(dict(mode='laptop', tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(api.shell, 0)
        display.restore()
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 0)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)

    def test_restore_returns_to_a_user_existing_tablet_mode_and_lock(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=1)
        api.shell = 1
        display = self.make(api, journal)
        display.apply(dict(mode='laptop', tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(api.shell, 0)
        self.assertEqual(journal.document['original_shell_mode'], 1)
        display.restore()
        self.assertEqual(api.shell, 1)
        self.assertEqual(api.current.auto_rotation, 1)
        self.assertIsNone(journal.document)

    def test_crash_recovery_restores_rotation_shell_and_rotation_lock(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        restarted = self.make(api, journal)
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 0)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)
        self.assertIsNone(restarted.owned)

    def test_failed_rotation_rolls_back_shell_rotation_and_lock(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        api.fail = True
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 0)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)

    def test_restore_preserves_external_rotation_and_rejects_replacement_panels(self):
        api, journal = Api(), MemoryJournal()
        display = self.make(api, journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        api.current = replace(api.current, transform=1)
        display.restore()
        self.assertEqual(api.current.transform, 1)
        self.assertEqual(api.writes, [2])
        self.assertEqual(api.shell, 0)

        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        api.current = replace(api.current, identity='replacement')
        with self.assertRaises(DisplayError):
            display.restore()
        self.assertIsNotNone(journal.document)
        self.assertEqual(api.writes, [2, 2])

    def test_hardware_handoff_locks_switches_tablet_then_releases_rotation(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        display.apply(dict(mode='hardware', tablet=True, rotation=None, disable_internal_input=False))
        self.assertEqual(api.events, [('lock', True), ('shell', 1), ('lock', False)])
        self.assertEqual(api.current.transform, 0)
        self.assertIsNone(journal.document['written_transform'])
        self.assertEqual(journal.document['written_shell_mode'], 1)

        api.current = replace(api.current, transform=2)
        display.hold_orientation()
        self.assertEqual(api.current.transform, 2)
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertEqual(journal.document['original_transform'], 2)

        display.apply(dict(mode='stand', tablet=True, rotation=0, disable_internal_input=False))
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 1)
        display.restore()
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(api.shell, 0)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)

    def test_physical_takeover_during_restart_keeps_native_direction(self):
        api, journal = Api(), MemoryJournal()
        physical = [False]
        query = lambda: (physical[0], HardwareStatus.OK)
        display = self.make(api, journal, query)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        physical[0] = True
        api.current = replace(api.current, transform=2, auto_rotation=0)
        api.shell = 0
        restarted = self.make(api, journal, query)
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(api.shell, 1)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)
        self.assertIsNone(restarted.owned)

    def test_physical_unknown_or_takeover_before_write_cannot_start_a_fixed_mode(self):
        for hardware, status in ((True, HardwareStatus.OK), (None, HardwareStatus.AMBIGUOUS)):
            api, journal = Api(), MemoryJournal()
            display = self.make(api, journal, lambda: (hardware, status))
            with self.assertRaises(DisplayError):
                display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
            self.assertEqual(api.writes, [])
            self.assertFalse(api.lock_writes)
            self.assertIsNone(journal.document)

    def test_late_native_takeover_is_rolled_back_instead_of_confirmed(self):
        class Race(Api):
            def rotate(self, *args):
                super().rotate(*args)
                self.current = replace(self.current, auto_rotation=0)
        api, journal = Race(), MemoryJournal()
        display = self.make(api, journal)
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        self.assertEqual(api.writes, [2, 0])
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 0)
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

    def test_native_lock_acquisition_and_restoration_are_read_back(self):
        api = object.__new__(NativeDisplay)
        state = [panel(auto_rotation=0)]
        api.read = lambda: state[0]
        def set_auto_rotation(enabled):
            state[0] = replace(state[0], auto_rotation=0 if enabled else 1)
            return True
        api._set_auto_rotation = set_auto_rotation
        api.lock(True)
        self.assertTrue(bool(state[0].auto_rotation & 1))
        api.lock(False)
        self.assertFalse(bool(state[0].auto_rotation & 1))

    def test_native_lock_fails_closed_without_the_shell_export(self):
        api = object.__new__(NativeDisplay)
        api.read = lambda: panel(auto_rotation=0)
        api._set_auto_rotation = None
        with self.assertRaises(DisplayError):
            api.lock(True)

    def test_native_lock_write_failure_is_not_confirmed(self):
        api = object.__new__(NativeDisplay)
        api.read = lambda: panel(auto_rotation=0)
        api._set_auto_rotation = lambda enabled: False
        with self.assertRaises(DisplayError):
            api.lock(True)

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
