"""Tests for service-level hardware switch observation/debouncing."""

from __future__ import annotations

import unittest
from unittest import mock

from panasonic_posture.model import HardwareStatus
from panasonic_posture.service.hardware_monitor import (
    HardwareMonitor,
    HardwareObservation,
)


class HardwareMonitorTests(unittest.TestCase):
    def make(self, *, debounce: float = 0.5) -> HardwareMonitor:
        self.result = (False, HardwareStatus.OK)
        return HardwareMonitor(
            lambda: self.result,
            debounce_seconds=debounce,
            logger=mock.Mock(),
        )

    def test_valid_value_is_effective_and_updates_last_valid(self) -> None:
        monitor = self.make()
        self.result = (True, HardwareStatus.OK)
        observation = monitor.poll(10.0)
        self.assertEqual(
            observation, HardwareObservation(True, True, HardwareStatus.OK)
        )
        self.assertIs(monitor.last_valid, True)
        self.assertIs(monitor.status, HardwareStatus.OK)

    def test_transient_unknown_keeps_last_value_during_debounce(self) -> None:
        monitor = self.make(debounce=0.5)
        monitor.poll(10.0)
        self.result = (None, HardwareStatus.READ_ERROR)
        observation = monitor.poll(10.2)
        self.assertEqual(observation.raw, None)
        self.assertEqual(observation.effective, False)
        self.assertIs(observation.status, HardwareStatus.READ_ERROR)
        observation = monitor.poll(10.49)
        self.assertEqual(observation.effective, False)

    def test_unknown_after_debounce_has_no_effective_value(self) -> None:
        monitor = self.make(debounce=0.5)
        monitor.poll(10.0)
        self.result = (None, HardwareStatus.READ_ERROR)
        self.assertEqual(monitor.poll(10.2).effective, False)
        self.assertIsNone(monitor.poll(10.71).effective)
        self.assertIs(monitor.last_valid, False)

    def test_query_exception_degrades_to_read_error(self) -> None:
        monitor = self.make()
        monitor.poll(10.0)

        def fail():
            raise OSError("switch vanished")

        monitor._query = fail
        observation = monitor.poll(10.1)
        self.assertEqual(observation.effective, False)
        self.assertIs(observation.status, HardwareStatus.READ_ERROR)
        monitor._log.exception.assert_called_once()

    def test_non_boolean_value_is_treated_as_unknown(self) -> None:
        monitor = self.make(debounce=0.0)
        self.result = ("yes", HardwareStatus.OK)
        observation = monitor.poll(10.0)
        self.assertIsNone(observation.raw)
        self.assertIsNone(observation.effective)
        self.assertIs(observation.status, HardwareStatus.OK)

    def test_recovery_clears_unknown_window(self) -> None:
        monitor = self.make()
        monitor.poll(10.0)
        self.result = (None, HardwareStatus.PERMISSION)
        monitor.poll(10.2)
        self.result = (True, HardwareStatus.OK)
        observation = monitor.poll(10.3)
        self.assertEqual(observation.effective, True)
        self.assertIs(observation.status, HardwareStatus.OK)
        self.result = (None, HardwareStatus.READ_ERROR)
        self.assertEqual(monitor.poll(10.31).effective, True)
        self.assertIsNone(monitor.poll(10.82).effective)

    def test_reset_drops_last_valid_and_starts_new_debounce(self) -> None:
        monitor = self.make()
        monitor.poll(10.0)
        monitor.reset()
        self.assertIsNone(monitor.last_valid)
        self.assertEqual(monitor.status, HardwareStatus.ABSENT)
        self.result = (None, HardwareStatus.READ_ERROR)
        observation = monitor.poll(10.1)
        self.assertIsNone(observation.effective)


if __name__ == "__main__":
    unittest.main()
