"""Concrete accelerometer sample sources used by :mod:`.sensor`.

The manager owns source selection and retries.  These adapters keep only the
mechanics of opening, reading, scaling and closing one source, which makes the
IIO iterator and the slow sysfs fallback independently testable.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..model import SensorSource

SYSFS_SLOW_READ_SECONDS = 1.8
IIO_DEVICE_GLOB = "iio:device*"
DEFAULT_ACCEL_NAME = "accel_3d"
LOG = logging.getLogger(__name__)


class SourceUnavailable(RuntimeError):
    """A sensor source cannot be opened or produced no usable samples."""


def _vector3(values: Iterable[Any], source: str) -> tuple[float, float, float]:
    """Convert a sample to the fixed three-axis contract used by callers."""

    try:
        vector = tuple(float(value) for value in values)
    except (TypeError, ValueError) as error:
        raise SourceUnavailable(f"{source}: invalid sample vector") from error
    if len(vector) != 3:
        raise SourceUnavailable(
            f"{source}: expected 3 sample components, got {len(vector)}"
        )
    return vector[0], vector[1], vector[2]


@runtime_checkable
class SampleSource(Protocol):
    """Minimal interface implemented by the real and fake sample sources."""

    kind: str

    def open(self) -> None: ...

    def read(self) -> tuple[float, float, float] | None: ...

    def close(self) -> None: ...


class IioSampleSource:
    """Adapt an ``hardware.iio.Accel`` iterator to the :class:`SampleSource` API."""

    kind = str(SensorSource.IIO)

    def __init__(self, accel: Any) -> None:
        self._accel: Any = accel
        self._iterator: Any = None
        self._entered = False

    def open(self) -> None:
        try:
            if hasattr(self._accel, "__enter__"):
                self._accel.__enter__()
                self._entered = True
            self._iterator = iter(self._accel)
        except Exception as error:
            self.close()
            raise SourceUnavailable(f"IIO: {error}") from error

    def read(self) -> tuple[float, float, float] | None:
        if self._iterator is None:
            raise SourceUnavailable("IIO: not opened")
        try:
            sample = next(self._iterator)
        except StopIteration as error:
            raise SourceUnavailable("IIO: stream ended") from error
        except Exception as error:
            raise SourceUnavailable(f"IIO: {error}") from error
        return _vector3(sample, "IIO")

    def close(self) -> None:
        accel, self._accel = self._accel, None
        iterator, self._iterator = self._iterator, None
        self._entered = False
        if iterator is not None:
            try:
                iterator.close()
            except Exception:
                LOG.debug("cannot close IIO iterator", exc_info=True)
        if accel is not None and hasattr(accel, "__exit__"):
            try:
                accel.__exit__(None, None, None)
            except Exception:
                LOG.debug("cannot exit IIO context", exc_info=True)


class SysfsSampleSource:
    """Poll ``in_accel_*_raw`` sysfs attributes (slow but always available)."""

    kind = str(SensorSource.SYSFS)

    def __init__(
        self,
        profile: Any,
        *,
        root: str | Path = "/sys/bus/iio/devices",
        apply_mount: Callable[[Sequence[float], Sequence[float] | None], Iterable[Any]]
        | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profile = profile
        self._root = Path(root)
        self._apply_mount = apply_mount
        self._monotonic = monotonic
        self._scale = 1.0
        self._offset = 0.0
        self._device: Path | None = None
        self._matrix: tuple[float, ...] | None = None

    def _mount(self, values: list[float]) -> tuple[float, float, float]:
        mounted: Iterable[Any]
        if self._apply_mount is None:
            from ..hardware.profile import apply_mount

            mounted = apply_mount(values, self._matrix)
        else:
            mounted = self._apply_mount(values, self._matrix)
        return _vector3(mounted, "sysfs")

    def open(self) -> None:
        name = getattr(self._profile, "accel_name", None) or DEFAULT_ACCEL_NAME
        from ..hardware.iio import find_sysfs_accel

        try:
            device = find_sysfs_accel(self._root, name)
        except (OSError, RuntimeError) as error:
            raise SourceUnavailable(f"sysfs: {error}") from error
        try:
            scale = float(
                (device / "in_accel_scale")
                .read_text(encoding="utf-8", errors="replace")
                .strip()
            )
            offset_path = device / "in_accel_offset"
            offset = (
                float(
                    offset_path.read_text(encoding="utf-8", errors="replace").strip()
                )
                if offset_path.exists()
                else 0.0
            )
        except (OSError, ValueError) as error:
            raise SourceUnavailable(
                f"sysfs: cannot read sensor scale: {error}"
            ) from error
        if not math.isfinite(scale) or not math.isfinite(offset):
            raise SourceUnavailable("sysfs: non-finite sensor scale/offset")
        if scale == 0.0:
            # A zero scale would report an all-zero vector forever; treat the
            # device as unusable instead of publishing fake "free fall" data.
            raise SourceUnavailable("sysfs: zero sensor scale")
        self._scale = scale
        self._offset = offset
        self._device = device
        # Profile override first, then the device's mount matrix, then identity.
        matrix = getattr(self._profile, "mount_matrix", None)
        if matrix is None:
            from ..hardware.iio import read_iio_mount

            try:
                matrix = read_iio_mount(device)
            except ValueError:
                matrix = None
        self._matrix = (
            tuple(float(value) for value in matrix) if matrix else None
        )

    def read(self) -> tuple[float, float, float] | None:
        device = self._device
        if device is None:
            raise SourceUnavailable("sysfs: not opened")
        started = self._monotonic()
        raw: list[int] = []
        try:
            for axis in "xyz":
                raw.append(
                    int(
                        (device / f"in_accel_{axis}_raw")
                        .read_text(encoding="utf-8", errors="replace")
                        .strip()
                    )
                )
                # Bound the time a stale acquisition can occupy the worker.
                if self._monotonic() - started > SYSFS_SLOW_READ_SECONDS:
                    raise SourceUnavailable("sysfs: slow sensor acquisition")
        except (OSError, ValueError) as error:
            raise SourceUnavailable(f"sysfs: cannot read sample: {error}") from error
        values = [(value + self._offset) * self._scale for value in raw]
        return self._mount(values)

    def close(self) -> None:
        self._device = None
        self._matrix = None


__all__ = [
    "DEFAULT_ACCEL_NAME",
    "IIO_DEVICE_GLOB",
    "SYSFS_SLOW_READ_SECONDS",
    "IioSampleSource",
    "SampleSource",
    "SourceUnavailable",
    "SysfsSampleSource",
]
