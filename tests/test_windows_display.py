"""Windows native panel selection and journalled rotation, without a desktop."""
from dataclasses import replace
import unittest

from panasonic_posture.windows.display import (
    ExternalOnlyDisplay, Panel, select_external_only, select_panel, WindowsDisplay,
    NativeDisplay, DevMode, PathInfo, ModeInfo,
)
from panasonic_posture.session.failures import DisplayError
from panasonic_posture.session.ports import DisplayRefresh
try:
    from tests.windows_fixture import MemoryJournal
except ModuleNotFoundError:
    from windows_fixture import MemoryJournal
import ctypes as C
from types import SimpleNamespace
from panasonic_posture.model import HardwareStatus, StateSnapshot, Target
from panasonic_posture.windows.session_display import SessionHardwareQuery


def panel(**changes):
    return replace(Panel('panel-1', r'\\.\DISPLAY1', (1, 0, 0), 11, 0, 2880, 1920, 1), **changes)


class Api:
    def __init__(self):
        self.current = panel()
        self.writes = []
        self.lock_writes = []
        self.events = []
        self.fail = False
        self.shell = 1

    def read(self):
        return self.current

    def lock(self, locked):
        self.lock_writes.append(locked)
        self.events.append(('lock', locked))
        self.current = replace(self.current, auto_rotation=1 if locked else 0)

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
        external = panel(identity='external', source=r'\.\DISPLAY2', source_key=(1, 0, 1), technology=5)
        self.assertEqual(select_panel([external, panel()]), panel())
        for paths in ([], [external], [panel(), panel(identity='other')],
                      [panel(), replace(external, source_key=panel().source_key)],
                      [panel(identity='')], [panel(transform=8)]):
            with self.subTest(paths=paths), self.assertRaises(DisplayError):
                select_panel(paths)

    def test_external_only_layout_is_distinct_and_requires_valid_unique_outputs(self):
        external = panel(identity='external', source=r'\.\DISPLAY2', source_key=(1, 0, 1), technology=5)
        second = replace(external, identity='external-2', source=r'\.\DISPLAY3', source_key=(1, 0, 2))
        self.assertEqual(select_external_only([external, second]),
                         ('external', 'external-2'))
        for paths in ([], [panel()], [panel(), external],
                      [replace(external, identity='')],
                      [replace(external, transform=8)],
                      [external, replace(second, identity='external')]):
            with self.subTest(paths=paths), self.assertRaises(DisplayError):
                select_external_only(paths)

    def test_external_only_display_never_writes_and_rejects_topology_changes(self):
        class Api:
            identity = ('external',)
            def external_identity(self):
                return self.identity

        api = Api()
        display = ExternalOnlyDisplay(api)
        display.hold_orientation()
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertTrue(display.needs_refresh(Target(mode='tent')))
        self.assertFalse(display.needs_refresh(Target(mode='hardware')))
        display.refresh(None)
        display.apply(dict(mode='unavailable', tablet=None, rotation=None,
                           disable_internal_input=False))
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tablet', tablet=True, rotation=None,
                               disable_internal_input=False))
        api.identity = ('replacement',)
        with self.assertRaises(DisplayError):
            display.refresh(None)
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='stand', tablet=True, rotation=0,
                               disable_internal_input=False))
        display.restore()
        display.apply(dict(mode='laptop', tablet=False, rotation=0,
                           disable_internal_input=False))

    def test_legacy_shell_journal_is_migrated_without_touching_shell(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(transform=2, auto_rotation=1)
        api.shell = 0
        journal.document = {
            'version': 1,
            'backend': 'windows',
            'spec': ['panel-1'],
            'original_transform': 0,
            'original_lock': False,
            'written_lock': True,
            'written_transform': 2,
            'original_shell_mode': 1,
            'written_shell_mode': 0,
        }
        display = self.make(api, journal)
        self.assertEqual(api.current.transform, 0)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertEqual(api.shell, 0)
        self.assertIsNone(journal.document)
        self.assertIsNone(display.owned)

    def test_fixed_modes_lock_rotate_and_restore_without_touching_shell(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)

        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertEqual(api.events, [('lock', True), ('rotate', 2)])
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(journal.document['original_lock'], False)
        self.assertNotIn('original_shell_mode', journal.document)
        self.assertEqual(journal.document['written_lock'], True)
        self.assertEqual(journal.document['written_transform'], 2)

        display.apply(dict(mode='stand', tablet=True, rotation=0,
                           disable_internal_input=False))
        self.assertEqual(api.current.transform, 0)
        display.apply(dict(mode='laptop', tablet=False, rotation=0,
                           disable_internal_input=False))
        display.restore()
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 1)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)

    def test_restore_returns_to_the_users_existing_rotation_lock(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=1)
        display = self.make(api, journal)
        display.apply(dict(mode='laptop', tablet=False, rotation=0,
                           disable_internal_input=False))
        display.restore()
        self.assertEqual(api.current.auto_rotation, 1)
        self.assertIsNone(journal.document)

    def test_crash_recovery_restores_rotation_and_lock_only(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        restarted = self.make(api, journal)
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 1)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)
        self.assertIsNone(restarted.owned)

    def test_unknown_physical_source_keeps_crash_journal_without_a_recovery_write(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(transform=2, auto_rotation=1)
        journal.document = {
            'version': 1,
            'backend': 'windows',
            'spec': ['panel-1'],
            'original_transform': 0,
            'original_lock': False,
            'written_lock': True,
            'written_transform': 2,
        }
        display = self.make(
            api, journal, lambda: (None, HardwareStatus.ABSENT)
        )
        self.assertEqual(api.writes, [])
        self.assertEqual(api.lock_writes, [])
        self.assertIsNotNone(journal.document)
        self.assertIsNotNone(display.owned)

    def test_failed_rotation_rolls_back_rotation_and_lock(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        api.fail = True
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tent', tablet=True, rotation=180,
                               disable_internal_input=False))
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 1)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)

    def test_restore_preserves_external_rotation_and_rejects_replacement_panels(self):
        api, journal = Api(), MemoryJournal()
        display = self.make(api, journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        api.current = replace(api.current, transform=1)
        display.restore()
        self.assertEqual(api.current.transform, 1)
        self.assertEqual(api.writes, [2])

        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        api.current = replace(api.current, identity='replacement')
        with self.assertRaises(DisplayError):
            display.restore()
        self.assertIsNotNone(journal.document)
        self.assertEqual(api.writes, [2, 2])

    def test_hardware_handoff_stays_unlocked_until_the_target_applies(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        display.apply(dict(mode='hardware', tablet=True, rotation=None,
                           disable_internal_input=False))
        self.assertEqual(api.events, [('lock', True), ('lock', False)])
        self.assertEqual(api.current.transform, 0)
        self.assertIsNone(journal.document['written_transform'])
        self.assertNotIn('written_shell_mode', journal.document)

        api.current = replace(api.current, transform=2)
        display.hold_orientation()
        self.assertEqual(api.current.transform, 2)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertEqual(journal.document['original_transform'], 2)

        display.apply(dict(mode='stand', tablet=True, rotation=0,
                           disable_internal_input=False))
        self.assertEqual(api.current.transform, 0)
        display.restore()
        self.assertEqual(api.current.transform, 2)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)

    def test_hardware_release_adopts_the_platform_direction_without_rewinding(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        display.apply(dict(mode='hardware', tablet=True, rotation=None,
                           disable_internal_input=False))

        api.current = replace(api.current, transform=0)
        display.hold_orientation()
        self.assertEqual(api.current.transform, 0)
        self.assertFalse(bool(api.current.auto_rotation & 1))

        # Leaving physical tablet mode may let the shell finish a direction
        # handoff after the first hold reading. The bounded pending-posture
        # window adopts that platform result instead of rotating back to the
        # first sample and then immediately rotating again for tent.
        api.current = replace(api.current, transform=2)
        api.writes.clear()
        display.hold_orientation()
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(api.writes, [])
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertEqual(journal.document['original_transform'], 2)
        self.assertIsNone(journal.document['written_transform'])

        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertEqual(api.current.transform, 2)
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertEqual(api.writes, [])

    def test_physical_release_preparation_rotates_to_the_pending_target(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        physical = [True]
        display = self.make(
            api,
            journal,
            lambda: (physical[0], HardwareStatus.OK),
        )
        display.apply(dict(mode='hardware', tablet=True, rotation=None,
                           disable_internal_input=False))
        self.assertEqual(api.current.transform, 0)

        api.writes.clear()
        display.prepare_hardware_release('tent')
        self.assertEqual(api.current.transform, 2)
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertEqual(journal.document['original_transform'], 0)
        self.assertEqual(journal.document['written_transform'], 2)
        self.assertEqual(api.writes, [2])

        # The release transaction now starts from the final direction, so the
        # platform cannot produce a second visible orientation after CSM.
        physical[0] = False
        display.hold_orientation()
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertEqual(api.current.transform, 2)
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertEqual(api.writes, [2])

    def test_gate_release_mid_preparation_keeps_the_prelocked_transaction(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        gate = [True, False]
        display = self.make(
            api,
            journal,
            lambda: (gate.pop(0) if gate else False, HardwareStatus.OK),
        )
        display.apply(dict(mode='hardware', tablet=True, rotation=None,
                           disable_internal_input=False))
        api.writes.clear()
        api.lock_writes.clear()

        with self.assertRaises(DisplayError) as raised:
            display.prepare_hardware_release('tent')
        self.assertEqual(raised.exception.detail, 'physical-tablet-took-over')
        # The switch released between the cached snapshot and the rotation
        # write. The lock and the pending direction stay owned so the released
        # posture is applied with exactly one rotation instead of rolling the
        # pre-lock back and rotating again after the release.
        self.assertEqual(api.current.transform, 0)
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertEqual(api.writes, [])
        self.assertEqual(journal.document['written_lock'], True)
        self.assertIsNotNone(display.owned)

        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(api.writes, [2])

    def test_transient_state_read_loss_does_not_rollback_a_prelocked_release(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        physical = [True]
        reads = []

        def load_state():
            reads.append(True)
            if len(reads) == 3:
                raise OSError(233, 'hardware pipe write failed')
            return StateSnapshot(
                instance='fixture',
                hardware=physical[0],
                hardware_status=HardwareStatus.OK,
            )

        display = self.make(
            api,
            journal,
            SessionHardwareQuery(load_state, sleep=lambda _delay: None),
        )
        display.apply(dict(mode='hardware', tablet=True, rotation=None,
                           disable_internal_input=False))
        api.writes.clear()
        api.lock_writes.clear()
        display.prepare_hardware_release('tent')

        physical[0] = False
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))

        self.assertEqual(api.current.transform, 2)
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertEqual(api.writes, [2])
        self.assertEqual(api.lock_writes, [True])

    def test_refresh_reclaims_auto_rotation_without_rewriting_the_direction(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        target = Target(mode='tent', tablet=True, rotation=180,
                        disable_internal_input=False)
        self.assertIsInstance(display, DisplayRefresh)
        display.apply(target)
        api.writes.clear()
        api.lock_writes.clear()
        api.events.clear()

        # Exiting or notifying the Windows shell can re-enable automatic
        # rotation after the direction transaction completed.
        api.current = replace(api.current, auto_rotation=0)
        state = SimpleNamespace(
            hardware=False,
            hardware_status=HardwareStatus.OK,
            target=target,
        )
        self.assertTrue(display.needs_refresh(target))
        display.refresh(state)

        self.assertEqual(api.writes, [])
        self.assertEqual(api.lock_writes, [True])
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertEqual(api.current.transform, 2)

    def test_refresh_restores_direction_after_a_late_platform_change(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        target = Target(mode='tent', tablet=True, rotation=180,
                        disable_internal_input=False)
        display.apply(target)
        api.writes.clear()
        api.lock_writes.clear()
        api.events.clear()

        api.current = replace(api.current, transform=0, auto_rotation=0)
        display.refresh(SimpleNamespace(
            hardware=False,
            hardware_status=HardwareStatus.OK,
            target=target,
        ))

        self.assertEqual(api.current.transform, 2)
        self.assertEqual(api.writes, [2])
        self.assertEqual(api.lock_writes, [True])
        self.assertTrue(bool(api.current.auto_rotation & 1))

    def test_tent_transition_reclaims_a_lock_windows_restored_before_apply(self):
        api, journal = Api(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal)
        display.apply(dict(mode='hardware', tablet=True, rotation=None,
                           disable_internal_input=False))
        self.assertFalse(bool(api.current.auto_rotation & 1))

        # While the switch is opening, Windows can restore automatic rotation
        # before the settled tent target arrives. The journal still records
        # the hardware unlock, so a no-op lock request must adopt the new
        # platform state as transaction ownership rather than leave stale
        # ownership that can disable the lock after a later restore.
        api.current = replace(api.current, auto_rotation=1)
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertEqual(api.current.transform, 2)
        self.assertTrue(bool(api.current.auto_rotation & 1))
        self.assertTrue(journal.document['written_lock'])

        display.restore()
        self.assertEqual(api.current.transform, 0)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)

    def test_failed_physical_release_readback_restores_the_original_lock(self):
        class ReadbackFailure(Api):
            def __init__(self):
                super().__init__()
                self.reads = 0

            def read(self):
                self.reads += 1
                if self.reads == 2:
                    raise OSError('readback failed')
                return super().read()

        api, journal = ReadbackFailure(), MemoryJournal()
        api.current = panel(auto_rotation=0)
        display = self.make(api, journal, lambda: (True, HardwareStatus.OK))
        with self.assertRaisesRegex(OSError, 'readback failed'):
            display.prepare_hardware_release('tent')
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertEqual(api.lock_writes, [True, False])
        self.assertIsNone(journal.document)
        self.assertIsNone(display.owned)

    def test_physical_takeover_during_restart_keeps_native_direction(self):
        api, journal = Api(), MemoryJournal()
        physical = [False]
        query = lambda: (physical[0], HardwareStatus.OK)
        display = self.make(api, journal, query)
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        physical[0] = True
        api.current = replace(api.current, transform=2, auto_rotation=0)
        restarted = self.make(api, journal, query)
        self.assertEqual(api.current.transform, 2)
        self.assertEqual(api.shell, 1)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document)
        self.assertIsNone(restarted.owned)

    def test_physical_takeover_before_write_hands_off_and_unknown_state_fails(self):
        api, journal = Api(), MemoryJournal()
        display = self.make(api, journal, lambda: (True, HardwareStatus.OK))
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertTrue(display.native_handoff_active)
        self.assertEqual(api.writes, [])
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document['written_transform'])

        api, journal = Api(), MemoryJournal()
        display = self.make(api, journal, lambda: (None, HardwareStatus.AMBIGUOUS))
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tent', tablet=True, rotation=180,
                               disable_internal_input=False))
        self.assertEqual(api.writes, [])
        self.assertFalse(api.lock_writes)
        self.assertIsNone(journal.document)

    def test_physical_takeover_mid_apply_hands_off_without_an_error(self):
        class Race(Api):
            def rotate(self, *args):
                super().rotate(*args)
                physical[0] = True

        api, journal = Race(), MemoryJournal()
        physical = [False]
        display = self.make(api, journal, lambda: (physical[0], HardwareStatus.OK))
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        self.assertTrue(display.native_handoff_active)
        self.assertEqual(api.current.transform, 2)
        self.assertFalse(bool(api.current.auto_rotation & 1))
        self.assertIsNone(journal.document['written_transform'])
        self.assertFalse(journal.document['written_lock'])

    def test_late_native_takeover_is_rolled_back_instead_of_confirmed(self):
        class Race(Api):
            def rotate(self, *args):
                super().rotate(*args)
                self.current = replace(self.current, auto_rotation=0)
        api, journal = Race(), MemoryJournal()
        display = self.make(api, journal)
        with self.assertRaises(DisplayError):
            display.apply(dict(mode='tent', tablet=True, rotation=180,
                               disable_internal_input=False))
        self.assertEqual(api.writes, [2, 0])
        self.assertEqual(api.current.transform, 0)
        self.assertEqual(api.shell, 1)
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
