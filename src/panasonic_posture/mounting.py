"""Pure sensor mounting transforms; acquisition chooses the matrix."""

import math
from typing import Sequence

# Real profiles use integer rotations, so a tight orthonormal tolerance is safe.
MOUNT_TOLERANCE = 1e-3

def parse_mount_matrix(value: object) -> tuple[float, ...] | None:
    """Validate a row-major 3x3 mount matrix.

    Raises :class:`ValueError` for wrong shapes, non-finite values or matrices
    that are not approximately orthonormal (those would make every attitude
    sample permanently invalid).
    """
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 9:
        raise ValueError("mount_matrix must be a list of 9 numbers")
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("mount_matrix entries must be numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError("mount_matrix entries must be finite")
        numbers.append(number)
    matrix = tuple(numbers)
    for row in range(3):
        for column in range(3):
            dot = sum(
                matrix[3 * row + offset] * matrix[3 * column + offset]
                for offset in range(3)
            )
            expected = 1.0 if row == column else 0.0
            if not math.isclose(dot, expected, rel_tol=0.0, abs_tol=MOUNT_TOLERANCE):
                raise ValueError("mount_matrix is not approximately orthonormal")
    return matrix


def apply_mount(
    vector: Sequence[float], matrix: Sequence[float] | None
) -> tuple[float, ...]:
    """Apply a row-major 3x3 mount matrix to an ``(x, y, z)`` vector."""
    if len(vector) != 3:
        raise ValueError("vector must have three components")
    if not matrix:
        return tuple(vector)
    if len(matrix) != 9:
        raise ValueError("mount matrix must have nine entries")
    return tuple(
        sum(matrix[3 * row + column] * vector[column] for column in range(3))
        for row in range(3)
    )
