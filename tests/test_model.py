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

    def test_from_dict_rejects_coercible_but_wrong_types(self):
        for target in (
            {"mode": "sideways"},
            {"mode": "stand", "tablet": 1},
            {"mode": "stand", "tablet": "true"},
            {"mode": "stand", "rotation": True},
            {"mode": "stand", "rotation": "0"},
            {"mode": "stand", "disable_internal_input": 1},
        ):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    Target.from_dict(target)

    def test_from_dict_accepts_exact_wire_types(self):
        target = Target.from_dict(
            {
                "mode": "stand",
                "tablet": True,
                "rotation": 0,
                "disable_internal_input": True,
            }
        )
        self.assertEqual(target, Target("stand", True, 0, True))


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
        self.assertTrue(data["tent_enabled"])
        self.assertTrue(data["tent_prompt"])
        self.assertFalse(data["stand_prompt"])
        self.assertEqual(data["target"]["mode"], "laptop")
        self.assertEqual(data["applied"]["rotation"], 0)
        self.assertEqual(data["sensor_source"], "iio")
        self.assertFalse(data["sensor_calibrated"])
        self.assertEqual(data["support_tier"], "compatible")
        self.assertIsNone(data["error_code"])
        self.assertIsNone(data["pending_prompt"])

    def test_json_round_trip(self):
        state = StateSnapshot(
            instance="x",
            generation=5,
            target=Target("tent", True, 180, True),
            support_tier="validated",
            tent_enabled=False,
            stand_prompt=True,
            posture_pending=True,
            sensor_calibrated=True,
            pending_notification=8,
            pending_prompt="stand",
        )
        text = dumps_state(state)
        self.assertNotIn(" ", text)  # compact encoding for D-Bus
        parsed = parse_state_snapshot(text)
        self.assertEqual(parsed.instance, "x")
        self.assertEqual(parsed.generation, 5)
        self.assertEqual(parsed.target, state.target)
        self.assertEqual(parsed.support_tier, "validated")
        self.assertFalse(parsed.tent_enabled)
        self.assertTrue(parsed.stand_prompt)
        self.assertTrue(parsed.posture_pending)
        self.assertTrue(parsed.sensor_calibrated)
        self.assertEqual(parsed.pending_notification, 8)
        self.assertEqual(parsed.pending_prompt, "stand")
        self.assertIsNone(parsed.applied)

    def test_legacy_state_defaults_to_no_posture_wait(self):
        state = StateSnapshot().to_dict()
        state.pop("posture_pending")
        self.assertFalse(parse_state_snapshot(json.dumps(state)).posture_pending)

    def test_version_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            loads_state(json.dumps({"version": 99}))
        with self.assertRaises(ValueError):
            loads_state("[]")

    def test_legacy_unsupported_state_without_tier_remains_unsupported(self):
        legacy = StateSnapshot(instance="old", supported=False).to_dict()
        legacy.pop("support_tier")
        parsed = parse_state_snapshot(json.dumps(legacy))
        self.assertFalse(parsed.supported)
        self.assertEqual(parsed.support_tier, "unsupported")

    def test_parser_rejects_wrong_wire_types_instead_of_coercing(self):
        base = StateSnapshot(instance="x", target=Target("tent", True, 180, True)).to_dict()
        cases = (
            ("generation", True),
            ("generation", -1),
            ("hardware", 1),
            ("tent_enabled", 1),
            ("posture_pending", 1),
            ("tent_prompt", 1),
            ("session_connected", "false"),
            ("pending_notification", True),
            ("pending_prompt", "sideways"),
            ("support_tier", "unknown"),
            ("target", []),
            ("target", {"mode": "sideways"}),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                document = dict(base)
                document[key] = value
                with self.assertRaises(ValueError):
                    parse_state_snapshot(json.dumps(document))


if __name__ == "__main__":
    unittest.main()
