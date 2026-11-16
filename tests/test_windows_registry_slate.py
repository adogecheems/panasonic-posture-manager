import sys
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock, patch

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.hardware.indicator_switch import IndicatorSwitch
from panasonic_posture.windows.hardware.slate_bootstrap import has_legacy_slate_journal
from panasonic_posture.windows.hardware.slate_journal import SlateJournal
from panasonic_posture.windows.service.registry_slate import RegistryPosture, RegistrySlateValue


class RegistryPostureTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.journal = SlateJournal(Path(tmp.name) / 'registry.json')
        self.values = [False]
        self.device = Mock()
        self.device.read.side_effect = lambda: self.values[-1]
        self.device.set.side_effect = self.values.append
        self.writer = RegistryPosture(self.device, self.journal)

    def test_same_target_and_service_restart(self):
        self.writer.apply(True)
        self.writer.apply(True)
        self.assertEqual(self.values, [False, True])
        RegistryPosture(self.device, self.journal).release(False)
        self.assertEqual(self.values, [False, True, False])
        self.assertIsNone(self.journal.load())

    def test_physical_fold_during_software_ownership(self):
        self.writer.apply(True)
        self.writer.release(True)
        self.assertEqual(self.values, [False, True])
        self.assertIsNone(self.journal.load())

    def test_readback_failure_preserves_ownership(self):
        self.device.set.side_effect = lambda _: None
        with self.assertRaises(OSError):
            self.writer.apply(True)
        self.assertIsNotNone(self.journal.load())

    def test_unknown_physical_state_defers_recovery_across_restart(self):
        self.writer.apply(True)
        recovered = RegistryPosture(self.device, self.journal)
        with self.assertRaises(OSError):
            recovered.release(None)
        self.assertEqual(self.values, [False, True])
        self.assertIsNotNone(self.journal.load())
        recovered.release(False)
        self.assertEqual(self.values, [False, True, False])

    def test_native_registry_value_and_type(self):
        value = RegistrySlateValue()
        api = Mock()
        api.OpenKey.return_value = nullcontext('key')
        with patch.dict(sys.modules, winreg=api):
            for raw, slate in ((0, True), (1, False)):
                with self.subTest(raw=raw):
                    api.QueryValueEx.return_value = raw, api.REG_DWORD
                    self.assertIs(value.read(), slate)
                    value.set(slate)
                    api.OpenKey.assert_called_with(api.HKEY_LOCAL_MACHINE,
                        r'SYSTEM\CurrentControlSet\Control\PriorityControl', 0, api.KEY_SET_VALUE)
                    api.SetValueEx.assert_called_with('key', 'ConvertibleSlateMode', 0, api.REG_DWORD, raw)
            for bad in ((2, api.REG_DWORD), (0, api.REG_SZ)):
                api.QueryValueEx.return_value = bad
                with self.assertRaises(OSError):
                    value.read()


class PhysicalIndicatorTests(unittest.TestCase):
    def start_blocking_indicator(self, readings, event_state):
        # Each read waits until the test chooses its completion relative to
        # invalidation or the fake clock. The final read stays in flight.
        entered = [threading.Event() for _ in readings]
        resume = [threading.Event() for _ in readings]
        device = Mock()

        def read():
            index = min(device.read.call_count - 1, len(readings) - 1)
            entered[index].set()
            if not resume[index].wait(2):
                raise OSError('test read timeout')
            return readings[index]

        device.read.side_effect = read
        events = Mock()
        events.query.return_value = event_state
        stamp = [1.]
        switch = IndicatorSwitch(events, open_device=lambda: nullcontext(device),
                                 legacy=lambda: False, clock=lambda: stamp[0])
        switch.start()
        self.addCleanup(switch.close)
        for latch in resume:
            self.addCleanup(latch.set)
        return switch, stamp, entered, resume

    def test_stale_inflight_read_cannot_cross_resume(self):
        switch, stamp, entered, resume = self.start_blocking_indicator(
            (True, False, False), (True, HardwareStatus.OK))
        self.assertTrue(entered[0].wait(2))
        switch.invalidate()
        resume[0].set()
        self.assertTrue(entered[1].wait(2))
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        resume[1].set()
        self.assertTrue(entered[2].wait(2))
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        # Freeze the next read so the previous sample can expire.
        stamp[0] += 2.
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        switch.events.query.return_value = None, HardwareStatus.READ_ERROR
        self.assertEqual(switch.query(), (None, HardwareStatus.READ_ERROR))

    def test_clean_indicator_tracks_physical_edges_without_etw_notifications(self):
        switch, _, entered, resume = self.start_blocking_indicator(
            (False, True, True), (None, HardwareStatus.ABSENT))
        for index, slate in enumerate((False, True)):
            resume[index].set()
            self.assertTrue(entered[index + 1].wait(2))
            self.assertEqual(switch.query(), (slate, HardwareStatus.OK))

    def test_legacy_guard_checks_presence_even_if_contents_are_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'profile/AppData/Local/PanasonicPostureManager'
            folder.mkdir(parents=True)
            self.assertFalse(has_legacy_slate_journal(root))
            (folder / 'native-display-slate.json').write_bytes(b'broken')
            self.assertTrue(has_legacy_slate_journal(root))

    def test_legacy_journal_uses_only_physical_events(self):
        events, device = Mock(), Mock()
        events.query.return_value = None, HardwareStatus.ABSENT
        switch = IndicatorSwitch(events, open_device=device, legacy=lambda: True)
        switch.start()
        self.addCleanup(switch.close)
        switch.worker.join(2)
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        events.query.return_value = True, HardwareStatus.OK
        self.assertEqual(switch.query(), (True, HardwareStatus.OK))
        device.assert_not_called()
