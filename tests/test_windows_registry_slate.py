import sys
import tempfile
import threading
import unittest
from pathlib import Path
from contextlib import nullcontext
from unittest.mock import Mock, patch

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.hardware.indicator_switch import IndicatorSwitch
from panasonic_posture.windows.hardware.convertible_slate import SlateDeviceAbsent, SlateError
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
    def start_blocking_indicator(self, readings):
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
        stamp = [1.]
        fallback = Mock()
        self.addCleanup(fallback.assert_not_called)
        switch = IndicatorSwitch(open_device=lambda: nullcontext(device), clock=lambda: stamp[0],
                                 fallback_factory=fallback)
        switch.start()
        self.addCleanup(switch.close)
        for latch in resume:
            self.addCleanup(latch.set)
        return switch, stamp, entered, resume

    def test_stale_inflight_read_cannot_cross_resume(self):
        switch, stamp, entered, resume = self.start_blocking_indicator(
            (True, False, False))
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

    def test_clean_indicator_tracks_physical_edges_without_etw_notifications(self):
        switch, _, entered, resume = self.start_blocking_indicator(
            (False, True, True))
        for index, slate in enumerate((False, True)):
            resume[index].set()
            self.assertTrue(entered[index + 1].wait(2))
            self.assertEqual(switch.query(), (slate, HardwareStatus.OK))

    def test_old_user_records_are_never_inspected(self):
        module = 'panasonic_posture.windows.hardware.slate_bootstrap'
        with patch(module + '._users_root', side_effect=PermissionError) as old, \
                patch(module + '.pending_slate_leases', side_effect=PermissionError) as pending:
            switch, _, entered, resume = self.start_blocking_indicator((True, True))
            resume[0].set()
            self.assertTrue(entered[1].wait(2))
            self.assertEqual(switch.query(), (True, HardwareStatus.OK))
            old.assert_not_called()
            pending.assert_not_called()

    def test_device_failure_closes_admission_without_starting_etw(self):
        attempted = threading.Event()

        def unavailable():
            attempted.set()
            raise OSError('device absent')

        switch = IndicatorSwitch(open_device=unavailable)
        switch.start()
        self.addCleanup(switch.close)
        self.assertTrue(attempted.wait(2))
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))

    def test_absent_device_starts_events_only_as_a_capability_fallback(self):
        events, factory = Mock(), Mock()
        factory.return_value = events
        events.query.return_value = None, HardwareStatus.ABSENT
        switch = IndicatorSwitch(open_device=Mock(side_effect=SlateDeviceAbsent()),
                                 fallback_factory=factory)
        switch.start()
        self.addCleanup(switch.close)
        switch.worker.join(2)
        factory.assert_called_once_with()
        events.start.assert_called_once_with()
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        events.query.return_value = True, HardwareStatus.OK
        self.assertEqual(switch.query(), (True, HardwareStatus.OK))
        switch.invalidate()
        events.invalidate.assert_called_once_with()
        events.query.return_value = None, HardwareStatus.READ_ERROR
        self.assertEqual(switch.query(), (None, HardwareStatus.READ_ERROR))
        switch.close()
        events.close.assert_called_once_with()

    def test_indicator_errors_do_not_enable_event_fallback(self):
        for error in (PermissionError(), SlateError('ambiguous'), ValueError('invalid byte')):
            with self.subTest(error=type(error).__name__):
                attempted = threading.Event()
                factory = Mock()

                def unavailable():
                    attempted.set()
                    raise error

                switch = IndicatorSwitch(open_device=unavailable, fallback_factory=factory)
                switch.start()
                self.addCleanup(switch.close)
                self.assertTrue(attempted.wait(2))
                self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
                switch.close()
                factory.assert_not_called()

    def test_fallback_startup_discards_sleep_era_events_and_closes_late_resources(self):
        for stopping in (False, True):
            with self.subTest(stopping=stopping):
                entered, finish = threading.Event(), threading.Event()
                events = Mock()
                events.query.return_value = None, HardwareStatus.ABSENT

                def start():
                    entered.set()
                    if not finish.wait(3):
                        raise OSError('test startup timeout')

                events.start.side_effect = start
                switch = IndicatorSwitch(open_device=Mock(side_effect=SlateDeviceAbsent()),
                                         fallback_factory=lambda: events)
                switch.start()
                self.addCleanup(switch.close)
                self.addCleanup(finish.set)
                self.assertTrue(entered.wait(2))
                if stopping:
                    switch.close()
                else:
                    switch.invalidate()
                finish.set()
                switch.worker.join(2)
                self.assertFalse(switch.worker.is_alive())
                if stopping:
                    events.close.assert_called_once_with()
                else:
                    events.invalidate.assert_called_once_with()

    def test_read_failure_after_open_keeps_the_selected_indicator_source(self):
        attempted = threading.Event()
        device, fallback = Mock(), Mock()

        def gone():
            attempted.set()
            raise SlateDeviceAbsent('device disconnected during read')

        device.read.side_effect = gone
        switch = IndicatorSwitch(open_device=lambda: nullcontext(device), fallback_factory=fallback)
        switch.start()
        self.addCleanup(switch.close)
        self.assertTrue(attempted.wait(2))
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        switch.close()
        fallback.assert_not_called()
