"""Tests for the privileged service composition root and lifecycle."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from panasonic_posture.controller import Controller
from panasonic_posture.model import HardwareStatus, SensorReading, SensorInfo
from panasonic_posture.service.ports import SessionOwner
from panasonic_posture.service.application import ServiceApplication
from panasonic_posture.service.hardware_monitor import HardwareMonitor
from panasonic_posture.service.settings import SettingsStore


class FakePresentation:
    def update(self, now, candidate, hardware, stamp):
        return candidate


class FakeSensor:
    info = SensorInfo()

    def __init__(self) -> None:
        self.started = 0
        self.closed = 0
        self.prefer_shared: list[bool] = []

    def start(self) -> None:
        self.started += 1

    def close(self) -> None:
        self.closed += 1

    def reading(self) -> SensorReading:
        return SensorReading()

    def set_prefer_shared(self, value: bool) -> None:
        self.prefer_shared.append(bool(value))


class FakeSwitch:
    def __init__(self) -> None:
        self.invalidated = 0
        self.reads = 0

    def read(self):
        self.reads += 1
        return False, HardwareStatus.OK

    def invalidate(self) -> None:
        self.invalidated += 1


class FakeActuator:
    def __init__(self) -> None:
        self.released = 0

    def release(self) -> None:
        self.released += 1


class FakeSettings:
    def load(self):
        return SimpleNamespace(tent_prompt=True)

    def save(self, settings) -> None:
        pass


class ApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.profile = SimpleNamespace(
            id="cfqv8",
            product="CFQV8",
            vendor="Panasonic Corporation",
            support_tier="validated",
            switch_name="Intel HID switches",
            accel_name="accel_3d",
            mount_matrix=None,
        )
        self.sensor = FakeSensor()
        self.switch = FakeSwitch()
        self.actuator = FakeActuator()
        self.controller = Controller()

    def make(self) -> ServiceApplication:
        return ServiceApplication.create(
            stop=threading.Event(),
            config_path=Path(self.directory.name) / "settings.json",
            development=False,
            instance="test-instance",
            profile=self.profile,
            controller=self.controller,
            presentation=FakePresentation(),
            settings=FakeSettings(),
            access=mock.Mock(),
            sensor=self.sensor,
            switch=self.switch,
            actuator=self.actuator,
        )

    def test_composition_wires_engine_and_monitor(self) -> None:
        application = self.make()
        self.assertIs(application.engine._sensor, self.sensor)
        self.assertIsInstance(application.engine._hardware_monitor, HardwareMonitor)
        self.assertEqual(application.engine.state.instance, "test-instance")
        self.assertEqual(application.engine.state.profile, "cfqv8")
        self.assertNotEqual(application.controller.generation, 0)

    def test_start_delegates_to_sensor(self) -> None:
        self.make().start()
        self.assertEqual(self.sensor.started, 1)

    def test_sleep_invalidates_switch_and_resets_engine(self) -> None:
        application = self.make()
        application.engine.session_owner = SessionOwner("linux-uid", "1000")
        application.sleep_event(True)
        self.assertEqual(self.switch.invalidated, 1)
        self.assertTrue(application.engine.suspended)
        self.assertIsNone(application.engine._hardware_monitor.last_valid)
        self.assertEqual(self.actuator.released, 1)

    def test_close_stops_engine_and_sensor(self) -> None:
        application = self.make()
        application.close()
        self.assertEqual(self.actuator.released, 1)
        self.assertEqual(self.sensor.closed, 1)

    def test_close_survives_engine_shutdown_failure(self) -> None:
        application = self.make()
        application.engine.shutdown = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch("panasonic_posture.service.application.LOG"):
            application.close()
        self.assertEqual(self.sensor.closed, 1)

    def test_injected_settings_store_is_not_replaced(self) -> None:
        store = SettingsStore(Path(self.directory.name) / "settings.json")
        application = ServiceApplication.create(
            stop=threading.Event(),
            profile=self.profile,
            settings=store,
            sensor=self.sensor,
            switch=self.switch,
            actuator=self.actuator,
            presentation=FakePresentation(),
            access=mock.Mock(),
        )
        self.assertIs(application.settings, store)


if __name__ == "__main__":
    unittest.main()
