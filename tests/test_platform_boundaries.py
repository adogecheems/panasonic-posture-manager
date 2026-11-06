"""Exercise the shared transaction pipeline with only Python's standard library."""

import subprocess
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace


class PlatformBoundaryTests(unittest.TestCase):
    def test_service_and_session_work_without_linux_adapters(self):
        result = subprocess.run(
            [sys.executable, "-S", str(Path(__file__).resolve())],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


def exercise_platform_boundaries():
    import time

    class BlockPlatformImports:
        def find_spec(self, name, path=None, target=None):
            if name in {
                "gi",
                "panasonic_posture.protocol",
                "panasonic_posture.linux_contract",
                "panasonic_posture.state_codec",
                "panasonic_posture.session.errors",
                "panasonic_posture.service.callbacks",
                "panasonic_posture.session.journal",
                "panasonic_posture.service.actuator",
                "panasonic_posture.service.access",
                "panasonic_posture.service.sensor",
                "panasonic_posture.service.settings",
                "panasonic_posture.session.desktop",
                "panasonic_posture.session.lockstate",
            } or name.startswith("panasonic_posture.hardware."):
                raise AssertionError(f"Unexpected platform import: {name}")

    sys.meta_path.insert(0, BlockPlatformImports())
    if hasattr(time, "CLOCK_BOOTTIME"):
        del time.CLOCK_BOOTTIME

    from panasonic_posture.controller import Controller
    from panasonic_posture.model import HardwareStatus, SensorReading, SensorInfo, DeviceInfo, Target
    from panasonic_posture.presentation import PosePresentation
    from panasonic_posture.service.engine import Engine
    from panasonic_posture.session.runtime import SessionRuntime
    from panasonic_posture.session.transaction import DisplayState, SoftwareDisplay
    from panasonic_posture.mounting import apply_mount
    from panasonic_posture.service.settings_schema import encode_settings, parse_settings
    from panasonic_posture.service.ports import Settings
    from panasonic_posture.tasks import InlineExecutor
    from panasonic_posture.service.ports import SessionOwner

    class InputControl:
        running = False
        state = None
        error_code = error_detail = None

        def keep(self, now):
            self.running = True
            self.state = SimpleNamespace(satisfies=lambda target: target.disable_internal_input, error=None)

        def follow_hardware(self, now):
            self.running = True
            self.state = SimpleNamespace(satisfies=lambda target: target.mode == "hardware", error=None)

        def release(self):
            self.running = False
            self.state = None

        def retry(self):
            self.release()

    class MemoryJournal:
        document = None

        def load(self):
            return self.document

        def save(self, document):
            self.document = document.copy()

        def clear(self):
            self.document = None

    class Display(SoftwareDisplay):
        backend_name = "test"

        def __init__(self):
            self.current = DisplayState(("panel",), 0, False, True)
            super().__init__(journal=MemoryJournal())

        def _read_state(self):
            return self.current

        def _write_rotation(self, state, transform):
            self.current = replace(self.current, transform=transform)

        def _write_lock(self, state, locked):
            self.current = replace(self.current, lock=locked)

    assert apply_mount((0, 0, -9.81), None) == (0, 0, -9.81)
    assert parse_settings(encode_settings(Settings())) == Settings()

    now = 100.0
    hardware = False
    actuator = InputControl()
    engine = Engine(
        controller=Controller(),
        sensor=SimpleNamespace(
            reading=lambda: SensorReading((0, 0, -9.81), now),
            info=SensorInfo(ready=True, backend="test-sensor", device="panel"),
            sensor_error=None,
        ),
        actuator=actuator,
        presentation=PosePresentation(),
        settings=None,
        device=DeviceInfo(platform="test"),
        hardware_query=lambda: (hardware, HardwareStatus.OK),
        monotonic=lambda: now,
        executor=InlineExecutor(),
    )
    display = Display()
    session = SessionRuntime(
        SimpleNamespace(
            get_state=lambda: engine.state,
            session_update=lambda *args: engine.session_update(*args, owner=SessionOwner("test", "user/session")),
        ),
        display,
        None,
        executor=InlineExecutor(),
    )

    assert engine.state.sensor_info.backend == "test-sensor"
    assert engine.state.sensor_source == "test-sensor"
    engine.set_mode("tent")
    engine.tick()
    assert engine.state.target.mode == "tent"
    assert engine.state.applied is None and not actuator.running
    session.tick(now)
    engine.tick()
    assert display.current.transform == 2 and display.current.lock is True
    assert actuator.running and engine.state.applied.mode == "tent"

    hardware = True
    engine.tick()
    session.tick(now)
    engine.tick()
    assert actuator.running and engine.state.applied.mode == "hardware"
    assert display.current.transform == 2 and display.current.lock is False

    now += 4
    engine.tick()
    assert engine.state.applied is None
    assert not actuator.running
    session.close()
    assert display.journal.load() is None
    engine.shutdown()

    # A per-output boolean lock must never be restored on a different panel.
    display.apply(Target(mode="tent", rotation=180))
    replacement = DisplayState(("different panel",), 2, True, True)
    display.current = replacement
    display.restore()
    assert display.current == replacement


if __name__ == "__main__":
    exercise_platform_boundaries()
