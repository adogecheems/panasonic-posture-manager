"""Tests for the pure accelerometer calibration helpers."""

from __future__ import annotations

import math
import unittest

from panasonic_posture.calibration import (
    CALIBRATION_STABILITY_DEGREES,
    REFERENCE_GRAVITY,
    apply_calibration,
    calibration_matrix_from_gravity,
    parse_calibration_matrix,
    stable_gravity_vector,
)
from panasonic_posture.model import SensorReading, SensorSource


def unit(vector):
    magnitude = math.sqrt(sum(component * component for component in vector))
    return tuple(component / magnitude for component in vector)


def reading(vector, stamp):
    return SensorReading(vector=vector, stamp=stamp, source=SensorSource.IIO)


class CalibrationMatrixTests(unittest.TestCase):
    def test_identity_for_expected_flat_direction(self):
        matrix = calibration_matrix_from_gravity((0.0, 0.0, -9.8))
        self.assertEqual(matrix, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))

    def test_maps_tilted_direction_to_reference_and_preserves_magnitude(self):
        measured = (2.0, -1.0, -9.7)
        matrix = calibration_matrix_from_gravity(measured)
        calibrated = apply_calibration(measured, matrix)
        self.assertAlmostEqual(calibrated[0], 0.0, places=6)
        self.assertAlmostEqual(calibrated[1], 0.0, places=6)
        self.assertAlmostEqual(calibrated[2], -math.sqrt(sum(v * v for v in measured)), places=6)
        parsed = parse_calibration_matrix(matrix)
        self.assertEqual(parsed, matrix)

    def test_opposite_direction_gets_a_proper_rotation(self):
        matrix = calibration_matrix_from_gravity((0.0, 0.0, 9.8))
        calibrated = apply_calibration((0.0, 0.0, 9.8), matrix)
        self.assertEqual(calibrated, (0.0, -0.0, -9.8))
        self.assertIsNotNone(parse_calibration_matrix(matrix))

    def test_apply_without_calibration_is_identity(self):
        self.assertEqual(apply_calibration((1.0, 2.0, 3.0), None), (1.0, 2.0, 3.0))

    def test_parse_rejects_invalid_matrices(self):
        for value in (
            [],
            [1, 0, 0, 0, 1, 0, 0, 0],
            [1, 0, 0, 0, 1, 0, 0, 0, "x"],
            [1, 0, 0, 0, 1, 0, 0, 0, float("nan")],
            [2, 0, 0, 0, 2, 0, 0, 0, 2],
            [1, 0, 0, 0, 1, 0, 0, 0, -1],
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_calibration_matrix(value)

    def test_invalid_gravity_direction_rejected(self):
        with self.assertRaises(ValueError):
            calibration_matrix_from_gravity((0.0, 0.0, 0.0))


class StableGravityVectorTests(unittest.TestCase):
    def test_uses_latest_stable_run(self):
        samples = [
            reading((8.0, 0.0, -2.0), 1.0),
            reading((0.1, 0.0, -9.8), 2.0),
            reading((0.2, 0.0, -9.8), 3.0),
            reading((0.15, 0.0, -9.8), 4.0),
        ]
        vector = stable_gravity_vector(samples)
        self.assertLess(abs(vector[0]), 0.05)
        self.assertLess(abs(vector[2] + 1.0), 0.01)

    def test_rejects_moving_or_missing_samples(self):
        with self.assertRaises(ValueError):
            stable_gravity_vector([])
        with self.assertRaises(ValueError):
            stable_gravity_vector(
                [
                    reading((0.0, 0.0, -9.8), 1.0),
                    reading((9.8, 0.0, 0.0), 2.0),
                    reading((0.0, 0.0, -9.8), 3.0),
                ]
            )

    def test_stability_boundary_is_documented(self):
        self.assertGreater(CALIBRATION_STABILITY_DEGREES, 0.0)
        self.assertEqual(unit(REFERENCE_GRAVITY), REFERENCE_GRAVITY)


if __name__ == "__main__":
    unittest.main()
