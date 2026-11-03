"""Pure sensor-orientation calibration helpers.

Calibration maps the gravity direction observed while the device is lying flat
in laptop form onto the canonical ``(0, 0, -1)`` direction used by the posture
policy.  The correction is a proper rotation, so sensor magnitudes and the
remaining attitude information are preserved.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from .model import SensorReading

REFERENCE_GRAVITY = (0.0, 0.0, -1.0)
CALIBRATION_WINDOW_SECONDS = 2.0
CALIBRATION_MIN_SAMPLES = 3
CALIBRATION_STABILITY_DEGREES = 8.0
CALIBRATION_MAX_FLAT_DEGREES = 45.0

_MATRIX_TOLERANCE = 1e-5
_VECTOR_EPSILON = 1e-9


def _components(vector: Sequence[float]) -> tuple[float, float, float]:
    try:
        values = tuple(float(component) for component in vector)
    except (TypeError, ValueError):
        raise ValueError("vector must contain three finite numbers") from None
    if len(values) != 3 or not all(math.isfinite(component) for component in values):
        raise ValueError("vector must contain three finite numbers")
    return values


def _normalize(vector: Sequence[float]) -> tuple[float, float, float]:
    x, y, z = _components(vector)
    magnitude = math.sqrt(x * x + y * y + z * z)
    if magnitude < _VECTOR_EPSILON:
        raise ValueError("vector has no direction")
    return x / magnitude, y / magnitude, z / magnitude


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross(
    left: Sequence[float], right: Sequence[float]
) -> tuple[float, float, float]:
    lx, ly, lz = _components(left)
    rx, ry, rz = _components(right)
    return (
        ly * rz - lz * ry,
        lz * rx - lx * rz,
        lx * ry - ly * rx,
    )


def _determinant(matrix: Sequence[float]) -> float:
    return (
        matrix[0] * (matrix[4] * matrix[8] - matrix[5] * matrix[7])
        - matrix[1] * (matrix[3] * matrix[8] - matrix[5] * matrix[6])
        + matrix[2] * (matrix[3] * matrix[7] - matrix[4] * matrix[6])
    )


def parse_calibration_matrix(value: object) -> tuple[float, ...] | None:
    """Validate a persisted row-major 3x3 calibration rotation."""

    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 9:
        raise ValueError("sensor calibration must be a 3x3 matrix")
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("sensor calibration entries must be numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError("sensor calibration entries must be finite")
        numbers.append(number)
    matrix = tuple(numbers)
    for row in range(3):
        for column in range(3):
            dot = sum(
                matrix[3 * row + offset] * matrix[3 * column + offset]
                for offset in range(3)
            )
            expected = 1.0 if row == column else 0.0
            if not math.isclose(dot, expected, rel_tol=0.0, abs_tol=_MATRIX_TOLERANCE):
                raise ValueError("sensor calibration is not orthonormal")
    if not math.isclose(
        _determinant(matrix), 1.0, rel_tol=0.0, abs_tol=_MATRIX_TOLERANCE
    ):
        raise ValueError("sensor calibration is not a proper rotation")
    return matrix


def calibration_matrix_from_gravity(vector: Sequence[float]) -> tuple[float, ...]:
    """Return a rotation mapping *vector* onto :data:`REFERENCE_GRAVITY`."""

    source = _normalize(vector)
    target = REFERENCE_GRAVITY
    cosine = max(-1.0, min(1.0, _dot(source, target)))
    axis = _cross(source, target)
    sine = math.sqrt(_dot(axis, axis))

    if sine < _VECTOR_EPSILON:
        if cosine > 0.0:
            return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        return (1.0, 0.0, 0.0, 0.0, -1.0, 0.0, 0.0, 0.0, -1.0)

    kx, ky, kz = (component / sine for component in axis)
    one_minus_cosine = 1.0 - cosine
    matrix = (
        cosine + kx * kx * one_minus_cosine,
        kx * ky * one_minus_cosine - kz * sine,
        kx * kz * one_minus_cosine + ky * sine,
        ky * kx * one_minus_cosine + kz * sine,
        cosine + ky * ky * one_minus_cosine,
        ky * kz * one_minus_cosine - kx * sine,
        kz * kx * one_minus_cosine - ky * sine,
        kz * ky * one_minus_cosine + kx * sine,
        cosine + kz * kz * one_minus_cosine,
    )
    return matrix


def apply_calibration(
    vector: Sequence[float], matrix: Sequence[float] | None
) -> tuple[float, float, float]:
    """Apply a calibration rotation to an accelerometer vector."""

    values = _components(vector)
    if matrix is None:
        return values
    parsed = parse_calibration_matrix(matrix)
    if parsed is None:  # pragma: no cover - defined only for type narrowing
        return values
    return tuple(
        sum(parsed[3 * row + column] * values[column] for column in range(3))
        for row in range(3)
    )


def _angle_degrees(left: Sequence[float], right: Sequence[float]) -> float:
    cosine = max(-1.0, min(1.0, _dot(left, right)))
    return math.degrees(math.acos(cosine))


def stable_gravity_vector(readings: Sequence[SensorReading]) -> tuple[float, float, float]:
    """Return the average direction of the latest stable sample run.

    The newest sample anchors the run.  Older samples are accepted while they
    stay within :data:`CALIBRATION_STABILITY_DEGREES`; a sudden movement
    therefore invalidates calibration until the device has settled again.
    """

    usable: list[tuple[float, ...]] = []
    for reading in readings:
        if not reading.valid or reading.vector is None:
            continue
        try:
            usable.append(_normalize(reading.vector))
        except ValueError:
            continue
    if len(usable) < CALIBRATION_MIN_SAMPLES:
        raise ValueError("not enough stable samples")

    latest = usable[-1]
    run: list[tuple[float, ...]] = []
    for unit in reversed(usable):
        if _angle_degrees(unit, latest) > CALIBRATION_STABILITY_DEGREES:
            break
        run.append(unit)
    if len(run) < CALIBRATION_MIN_SAMPLES:
        raise ValueError("sensor is still moving")

    average = tuple(
        sum(unit[axis] for unit in run) / len(run) for axis in range(3)
    )
    return _normalize(average)


__all__ = [
    "CALIBRATION_MAX_FLAT_DEGREES",
    "CALIBRATION_MIN_SAMPLES",
    "CALIBRATION_STABILITY_DEGREES",
    "CALIBRATION_WINDOW_SECONDS",
    "REFERENCE_GRAVITY",
    "apply_calibration",
    "calibration_matrix_from_gravity",
    "parse_calibration_matrix",
    "stable_gravity_vector",
]
