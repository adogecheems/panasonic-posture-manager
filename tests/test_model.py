"""Tests for the shared data model (Lead integration scope)."""

import json
import unittest

from panasonic_posture.model import (
    BackendState,
    HardwareStatus,
    Mode,
    Pose,
    SensorReading,
    SensorSource,
    StateSnapshot,
    Target,
    dumps_state,
    loads_state,
    parse_state_snapshot,
)
from panasonic_posture.protocol import STATE_VERSION


class TargetTests(unittest.TestCase):
    def test_round_trip(self):
        target = Target("tent", True, 180, True)
        self.assertEqual(Target.from_dict(target.to_dict()), target)

    def test_unavailable_target(self):
        target = Target("unavailable", None, None, False)
        self.assertEqual(Target.from_dict(target.to_dict()), target)

    def test_from_dict_rejects_non_object(self):
        with self.assertRaises(ValueError):
            Target.from_dict([])

    def test_from_dict_coerces_types(self):
        target = Target.from_dict({"mode": "stand", "tablet": 1, "rotation": 0,
                                   "disable_internal_input": 1})
        self.assertEqual(target.mode, "stand")
        self.assertIs(target.tablet, True)
        self.assertEqual(target.rotation, 0)
        self.assertTrue(target.disable_internal_input)


class SensorReadingTests(unittest.TestCase):
    def test_valid_reading(self):
        reading = SensorReading((0.0, 0.0, -9.80665), 1.0, SensorSource.IIO)
        self.assertTrue(reading.valid)
        self.assertAlmostEqual(reading.magnitude, 9.80665, places=3)

    def test_invalid_reading(self):
        for vector in (None, (0.0, 0.0), (float("nan"), 0.0, 0.0)):
            self.assertFalse(SensorReading(vector, 1.0).valid)

    def test_missing_stamp_is_invalid(self):
        self.assertFalse(SensorReading((0.0, 0.0, -9.8), None).valid)


class StateSnapshotTests(unittest.TestCase):
    def test_serializes_expected_schema(self):
        state = StateSnapshot(
            instance="abc",
            generation=3,
            pose=Pose.LAPTOP,
            hardware=False,
            hardware_status=HardwareStatus.ABSENT,
            manual=Mode.AUTO,
            target=Target("laptop", False, 0, False),
            applied=Target("laptop", False, 0, False),
            backend=BackendState.READY,
            session_connected=True,
            sensor_source=SensorSource.IIO,
        )
        data = state.to_dict()
        self.assertEqual(data["version"], STATE_VERSION)
        self.assertEqual(data["pose"], "laptop")
        self.assertEqual(data["manual"], "auto")
        self.assertEqual(data["target"]["mode"], "laptop")
        self.assertEqual(data["applied"]["rotation"], 0)
        self.assertEqual(data["sensor_source"], "iio")
        self.assertIsNone(data["error_code"])

    def test_json_round_trip(self):
        state = StateSnapshot(instance="x", generation=5, target=Target("tent", True, 180, True))
        text = dumps_state(state)
        self.assertNotIn(" ", text)  # compact encoding for D-Bus
        parsed = parse_state_snapshot(text)
        self.assertEqual(parsed.instance, "x")
        self.assertEqual(parsed.generation, 5)
        self.assertEqual(parsed.target, state.target)
        self.assertIsNone(parsed.applied)

    def test_version_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            loads_state(json.dumps({"version": 99}))
        with self.assertRaises(ValueError):
            loads_state("[]")


if __name__ == "__main__":
    unittest.main()
