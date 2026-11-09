"""Software-tablet transactions with explicit orientation mapping and fake OS."""
from dataclasses import replace
import unittest

from panasonic_posture.model import HardwareStatus, StateSnapshot, Target
from panasonic_posture.session.failures import DisplayError
from panasonic_posture.windows.display import Panel, WindowsDisplay
from panasonic_posture.windows.orientation import OrientationReading
from panasonic_posture.windows.simulation import MemoryJournal
from panasonic_posture.windows.tablet_display import WindowsTabletDisplay


class Api:
    def __init__(self):
        self.current = Panel('panel', 'source', (1, 0, 0), 11, 0, 2880, 1920, 1)
        self.writes = []
        self.after_write = lambda: None
    def read(self): return self.current
    def rotate(self, identity, previous, transform):
        if (identity, previous) != (self.current.identity, self.current.transform):
            raise DisplayError('display-identity-changed')
        self.writes.append(transform)
        self.current = replace(self.current, transform=transform)
        self.after_write()


class TabletDisplayTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.value = 4
        self.missing = False
        self.hardware, self.status = False, HardwareStatus.OK
        self.api, self.journal = Api(), MemoryJournal()
        self.display = WindowsTabletDisplay(journal=self.journal, api=self.api,
            orientation=lambda: None if self.missing else OrientationReading(self.value, self.now, 'sensor'),
            mapping=(0, 3, 2, 1), clock=lambda: self.now,
            hardware_query=lambda: (self.hardware, self.status))
        self.target = Target(mode='tablet', tablet=True, rotation=None, disable_internal_input=False)
        self.state = StateSnapshot(instance='fixture', target=self.target, hardware=False,
            hardware_status=HardwareStatus.OK, supported=True, session_connected=True)

    def stable(self, value):
        self.value = value
        self.now += .1
        self.display.refresh(self.state)
        self.now += .5
        self.display.refresh(self.state)

    def test_four_quadrants_use_explicit_mapping_and_restore_original_direction(self):
        self.display.apply(self.target)
        for value, expected in ((1, 3), (2, 2), (3, 1), (0, 0)):
            self.stable(value)
            self.assertEqual(self.api.current.transform, expected)
            self.assertEqual(self.journal.document['written_transform'], expected)
        self.stable(1)
        self.display.restore()
        self.assertEqual(self.api.current.transform, 0)
        self.assertIsNone(self.journal.document)

    def test_faces_and_unstable_samples_hold_without_writes(self):
        self.display.apply(self.target)
        self.stable(1)
        writes = list(self.api.writes)
        for value in (4, 5, 2, 3, 0):
            self.value, self.now = value, self.now + .1
            self.display.refresh(self.state)
        self.assertEqual(self.api.writes, writes)
        self.assertEqual(self.api.current.transform, 3)

    def test_cached_observation_does_not_advance_dwell(self):
        self.display.apply(self.target)
        self.value = 1
        self.display.refresh(self.state)
        for _ in range(8): self.display.refresh(self.state)
        self.assertEqual(self.api.writes, [])

    def test_missing_direction_cannot_start_or_keep_success(self):
        self.missing = True
        with self.assertRaises(DisplayError): self.display.apply(self.target)
        self.assertIsNone(self.journal.document)
        self.missing = False
        self.display.apply(self.target)
        self.stable(1)
        self.missing = True
        with self.assertRaises(DisplayError): self.display.refresh(self.state)
        self.assertEqual(self.api.current.transform, 0)
        self.assertFalse(self.display._tablet)

    def test_stale_direction_restores_and_retains_no_active_tablet(self):
        self.display.apply(self.target)
        self.stable(1)
        stamp = self.now
        self.display.orientation = lambda: OrientationReading(1, stamp, 'sensor')
        self.now += 3
        with self.assertRaises(DisplayError): self.display.refresh(self.state)
        self.assertEqual(self.api.current.transform, 0)
        self.assertFalse(self.display._tablet)

    def test_native_rotation_and_physical_switch_preempt_even_matching_direction(self):
        for takeover in ('native', 'physical'):
            with self.subTest(takeover=takeover):
                self.setUp()
                self.display.apply(self.target)
                self.stable(1)
                if takeover == 'native': self.api.current = replace(self.api.current, auto_rotation=0)
                else: self.hardware = True
                writes = list(self.api.writes)
                with self.assertRaises(DisplayError): self.display.refresh(self.state)
                self.assertEqual(self.api.writes, writes)
                self.assertIsNone(self.journal.document)
                self.assertFalse(self.display._tablet)

    def test_unknown_physical_state_keeps_recovery_journal_without_writing(self):
        self.display.apply(self.target)
        self.stable(1)
        writes = list(self.api.writes)
        self.hardware, self.status = None, HardwareStatus.AMBIGUOUS
        with self.assertRaises(DisplayError): self.display.refresh(self.state)
        self.assertEqual(self.api.writes, writes)
        self.assertIsNotNone(self.journal.document)
        self.hardware, self.status = False, HardwareStatus.OK
        self.display.restore()
        self.assertEqual(self.api.current.transform, 0)

    def test_external_rotation_is_new_baseline_before_subsequent_write(self):
        self.display.apply(self.target)
        self.stable(1)
        self.api.current = replace(self.api.current, transform=1)
        self.stable(2)
        self.assertEqual(self.api.current.transform, 2)
        self.assertEqual(self.journal.document['original_transform'], 1)
        self.display.restore()
        self.assertEqual(self.api.current.transform, 1)

    def test_crash_during_dynamic_rotation_uses_existing_recovery_transaction(self):
        self.display.apply(self.target)
        def interrupted():
            self.api.after_write = lambda: None
            raise SystemExit('simulated host death before read-back')
        self.api.after_write = interrupted
        with self.assertRaises(SystemExit): self.stable(1)
        self.assertEqual(self.journal.document['pending_transform'], 3)
        WindowsDisplay(api=self.api, journal=self.journal, hardware_query=lambda: (False, HardwareStatus.OK))
        self.assertEqual(self.api.current.transform, 0)
        self.assertIsNone(self.journal.document)

    def test_replacement_panel_cannot_inherit_old_dynamic_ownership(self):
        self.display.apply(self.target)
        self.stable(1)
        writes = list(self.api.writes)
        self.api.current = replace(self.api.current, identity='replacement')
        with self.assertRaises(DisplayError): self.display.refresh(self.state)
        self.assertEqual(self.api.writes, writes)
        self.assertIsNotNone(self.journal.document)
        self.assertFalse(self.display._tablet)

    def test_new_orientation_device_during_mode_cannot_continue_rotation(self):
        self.display.apply(self.target)
        self.stable(1)
        self.display.orientation = lambda: OrientationReading(1, self.now, 'replacement-sensor')
        with self.assertRaises(DisplayError): self.display.refresh(self.state)
        self.assertEqual(self.api.current.transform, 0)

    def test_failed_ownership_save_never_writes_a_new_quadrant(self):
        self.display.apply(self.target)
        self.value = 1
        self.display.refresh(self.state)
        self.now += .5
        self.journal.save = lambda document: (_ for _ in ()).throw(OSError('full'))
        with self.assertRaises(OSError): self.display.refresh(self.state)
        self.assertEqual(self.api.writes, [])
        self.assertFalse(self.display._tablet)

    def test_native_takeover_during_write_is_not_reverted_or_confirmed(self):
        self.display.apply(self.target)
        self.api.after_write = lambda: setattr(self.api, 'current', replace(self.api.current, auto_rotation=0))
        with self.assertRaises(DisplayError): self.stable(1)
        self.assertEqual(self.api.writes, [3])
        self.assertEqual(self.api.current.transform, 3)
        self.assertIsNone(self.journal.document)

    def test_readback_failure_rolls_back_using_pending_journal(self):
        self.display.apply(self.target)
        def failed_readback():
            self.api.after_write = lambda: None
            raise DisplayError('display-transform-not-applied')
        self.api.after_write = failed_readback
        with self.assertRaises(DisplayError): self.stable(1)
        self.assertEqual(self.api.current.transform, 0)
        self.assertIsNone(self.journal.document)

    def test_unqualified_snapshot_is_rejected_and_switching_fixed_mode_stops_refresh(self):
        self.display.apply(self.target)
        with self.assertRaises(DisplayError):
            self.display.refresh(replace(self.state, session_connected=False))
        self.assertFalse(self.display._tablet)
        self.display.apply(self.target)
        self.display.apply(Target(mode='tent', tablet=True, rotation=180, disable_internal_input=False))
        self.assertEqual(self.api.current.transform, 2)
        self.assertFalse(self.display._tablet)
        self.assertFalse(self.display.needs_refresh(Target(mode='tent')))


if __name__ == '__main__': unittest.main()
