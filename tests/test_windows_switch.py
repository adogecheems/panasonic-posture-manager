"""Physical GPIO observation must not accept an unqualified system metric."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.switch import WindowsSwitch


class SwitchTests(unittest.TestCase):
    def test_inverts_only_the_authenticated_physical_slate_metric(self):
        native = SimpleNamespace(candidates=lambda: ['ACPI\\INT33D3\\one'], metric=Mock(return_value=1))
        switch = WindowsSwitch(native)
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        native.metric.return_value = 0
        self.assertEqual(switch.query(), (True, HardwareStatus.OK))

    def test_missing_and_ambiguous_switches_never_consume_the_default_metric(self):
        for candidates, status in (([], HardwareStatus.ABSENT), (['one', 'two'], HardwareStatus.AMBIGUOUS)):
            native = SimpleNamespace(candidates=lambda: candidates, metric=Mock(return_value=0))
            self.assertEqual(WindowsSwitch(native).query(), (None, status))
            native.metric.assert_not_called()

    def test_replacement_does_not_inherit_the_original_device_identity(self):
        native = SimpleNamespace(candidates=Mock(return_value=['one']), metric=lambda: 1)
        switch = WindowsSwitch(native)
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        native.candidates.return_value = ['two']
        self.assertEqual(switch.query(), (None, HardwareStatus.READ_ERROR))

    def test_errors_and_invalid_metrics_fail_closed(self):
        for error, status in ((PermissionError(), HardwareStatus.PERMISSION), (OSError(), HardwareStatus.READ_ERROR)):
            native = SimpleNamespace(candidates=Mock(side_effect=error), metric=Mock())
            self.assertEqual(WindowsSwitch(native).query(), (None, status))
            native.metric.assert_not_called()
        for metric in (True, -1, 2, None, '0'):
            native = SimpleNamespace(candidates=lambda: ['one'], metric=lambda: metric)
            self.assertEqual(WindowsSwitch(native).query(), (None, HardwareStatus.READ_ERROR))


if __name__ == '__main__':
    unittest.main()
