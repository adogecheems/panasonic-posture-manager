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
    from panasonic_posture.model import HardwareStatus, SensorReading, SensorSource, Target
    from panasonic_posture.presentation import PosePresentation
    from panasonic_posture.service.engine import Engine
    from panasonic_posture.session.runtime import SessionRuntime
    from panasonic_posture.session.transaction import DisplayState, SoftwareDisplay
    from panasonic_posture.tasks import InlineExecutor

    class InputControl:
        running = False
        state = None
        error_code = error_detail = None

        def keep(self, now):
            self.running = True
            self.state = SimpleNamespace(tablet=True, input_disabled=True, error=None)

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

    now = 100.0
    hardware = False
    actuator = InputControl()
    engine = Engine(
        controller=Controller(),
        sensor=SimpleNamespace(
            reading=lambda: SensorReading((0, 0, -9.81), now),
            source=SensorSource.STARTING,
            sensor_error=None,
        ),
        actuator=actuator,
        presentation=PosePresentation(),
        settings=None,
        profile=None,
        hardware_query=lambda: (hardware, HardwareStatus.OK),
        monotonic=lambda: now,
        executor=InlineExecutor(),
    )
    display = Display()
    session = SessionRuntime(
        SimpleNamespace(
            get_state=lambda: engine.state,
            session_update=lambda *args: engine.session_update(*args, sender_uid=1),
        ),
        display,
        None,
        executor=InlineExecutor(),
    )

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
    assert not actuator.running and engine.state.applied.mode == "hardware"
    assert display.current.transform == 0 and display.current.lock is False

    now += 4
    engine.tick()
    assert engine.state.applied is None
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
