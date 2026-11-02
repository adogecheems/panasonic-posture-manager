"""IIO accelerometer buffer reader.

The buffer exposes the sensor hub at its native rate instead of the roughly
2 Hz obtained by polling the three sysfs raw attributes.  Sample types are
parsed from ``scan_elements`` and support signed/unsigned, little/big endian,
8/16/32/64-bit storage, bit masks, bit shifts and sign extension; the channel
order comes from ``in_accel_{x,y,z}_index`` instead of assuming x/y/z.
"""

from __future__ import annotations

import math
import os
import re
import select
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from ..protocol import IIO_NO_SAMPLE_SECONDS
from .profile import apply_mount

IIO_ROOT = Path("/sys/bus/iio/devices")
DEVICE_PREFIX = "iio:device"
DEFAULT_ACCEL_NAME = "accel_3d"
SAMPLE_WAIT_SECONDS = 0.5

_AXES = ("x", "y", "z")
ByteOrder = Literal["little", "big"]
_TYPE = re.compile(
    r"^(?:(?P<endian>le|be):)?(?P<sign>[su])(?P<bits>\d+)/(?P<storage>\d+)"
    r">>(?P<shift>\d+)$"
)


class Unavailable(RuntimeError):
    """The IIO source cannot provide samples right now (fall back to sysfs)."""


@dataclass(frozen=True)
class SampleType:
    """Decoded ``scan_elements`` type description."""

    byteorder: ByteOrder
    signed: bool
    bits: int  # number of meaningful bits
    storage: int  # storage width in bits (8/16/32/64)
    shift: int  # right shift applied to the storage word

    @property
    def size(self) -> int:
        """Storage size in bytes."""
        return self.storage // 8

    @property
    def mask(self) -> int:
        return (1 << self.bits) - 1

    def decode(self, data: bytes) -> int:
        """Decode one storage word, applying mask, shift and sign extension."""
        if len(data) != self.size:
            raise ValueError(
                f"axis sample is {len(data)} bytes, expected {self.size}"
            )
        raw = int.from_bytes(data, self.byteorder, signed=False)
        value = (raw >> self.shift) & self.mask
        if self.signed and self.bits > 0 and (value >> (self.bits - 1)) & 1:
            value -= 1 << self.bits
        return value


def parse_type(value: str) -> SampleType:
    """Parse an IIO ``*_type`` attribute such as ``le:s16/16>>0``."""
    text = (value or "").strip()
    match = _TYPE.match(text)
    if match is None:
        raise ValueError(f"unsupported IIO sample type {text!r}")
    storage = int(match.group("storage"))
    bits = int(match.group("bits"))
    shift = int(match.group("shift"))
    if storage not in (8, 16, 32, 64):
        raise ValueError(f"unsupported IIO storage width {storage}")
    if bits < 1 or bits > storage:
        raise ValueError(f"IIO sample has {bits} bits in {storage} bits of storage")
    if shift < 0 or shift + bits > storage:
        raise ValueError(f"IIO sample shift {shift} does not fit {storage} bits")
    byteorder: ByteOrder = (
        "big" if match.group("endian") == "be" else "little"
    )
    return SampleType(
        byteorder=byteorder,
        signed=match.group("sign") == "s",
        bits=bits,
        storage=storage,
        shift=shift,
    )


def decode_sample(data: bytes, types: Sequence[SampleType]) -> tuple[int, ...]:
    """Decode one buffer sample laid out in *types* order."""
    expected = sum(kind.size for kind in types)
    if len(data) != expected:
        raise ValueError(
            f"IIO sample is {len(data)} bytes, expected {expected}"
        )
    values: list[int] = []
    offset = 0
    for kind in types:
        values.append(kind.decode(data[offset : offset + kind.size]))
        offset += kind.size
    return tuple(values)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _optional_text(path: Path) -> str:
    try:
        return _read_text(path).strip()
    except OSError:
        return ""


def _write_text(path: Path, value: object) -> None:
    Path(path).write_text(f"{value}\n", encoding="utf-8")


def read_iio_mount(device: str | Path) -> tuple[float, ...] | None:
    """Read ``in_accel_mount_matrix``; ``None`` when absent or malformed."""
    path = Path(device) / "in_accel_mount_matrix"
    try:
        text = _read_text(path)
    except OSError:
        return None
    values: list[float] = []
    for token in re.split(r"[,;\s]+", text.strip()):
        if not token:
            continue
        try:
            value = float(token)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        values.append(value)
    return tuple(values) if len(values) == 9 else None


def scan_plan(
    device: str | Path,
) -> tuple[tuple[str, ...], tuple[SampleType, ...]]:
    """Return ``(axis_order, sample_types)`` in buffer layout order.

    ``in_accel_{x,y,z}_index`` defines the layout; when the attribute is not
    exposed (older kernels) the declared x/y/z order is assumed.
    """
    device = Path(device)
    scan = device / "scan_elements"
    indices: dict[str, int] = {}
    for axis in _AXES:
        text = _optional_text(scan / f"in_accel_{axis}_index")
        if not text:
            continue
        try:
            indices[axis] = int(text)
        except ValueError as exc:
            raise Unavailable(
                f"invalid IIO channel index for {axis}: {text!r}"
            ) from exc
    if indices:
        if set(indices) != set(_AXES):
            raise Unavailable(
                "IIO exposes a partial set of acceleration channel indices"
            )
        if len(set(indices.values())) != len(_AXES):
            raise Unavailable("IIO acceleration channel indices are not unique")
        axes = tuple(sorted(_AXES, key=lambda axis: indices[axis]))
    else:
        axes = _AXES

    types: list[SampleType] = []
    for axis in axes:
        text = _optional_text(scan / f"in_accel_{axis}_type")
        if not text:
            raise Unavailable(f"missing IIO scan element type for accel_{axis}")
        types.append(parse_type(text))
    return axes, tuple(types)


def _find_device(root: Path, name: str) -> Path:
    try:
        entries = sorted(root.glob(f"{DEVICE_PREFIX}*"))
    except OSError as exc:
        raise Unavailable(f"cannot enumerate {root}: {exc}") from exc
    matches = [device for device in entries if _optional_text(device / "name") == name]
    if not matches:
        raise Unavailable(f"{name} IIO device not found")
    if len(matches) > 1:
        # Mirror the sysfs fallback (which requires exactly one device) and the
        # C relay's ambiguity handling: never guess between two identical
        # sensors, because the mount matrix/orientation may differ.
        raise Unavailable(
            f"multiple {name} IIO devices found: "
            + ", ".join(device.name for device in matches)
        )
    return matches[0]


def _find_trigger(root: Path, name: str) -> Path | None:
    try:
        entries = sorted(root.glob("trigger*"))
    except OSError:
        return None
    for trigger in entries:
        if _optional_text(trigger / "name") == name:
            return trigger
    return None


def _default_open_device(path: str) -> int:
    return os.open(path, os.O_RDONLY | os.O_CLOEXEC)


class Accel:
    """One IIO accelerometer buffer session.

    Use as a context manager and iterate for ``(x, y, z)`` vectors in the
    profile/mount frame.  :meth:`close` stops the buffer and restores the
    sysfs state that existed before setup.
    """

    def __init__(
        self,
        stop,
        name: str | None = None,
        mount: Sequence[float] | None = None,
        *,
        root: str | Path | None = None,
        open_device: Callable[[str], int] | None = None,
    ) -> None:
        self.stop = stop
        self.name = name or DEFAULT_ACCEL_NAME
        self.root = Path(root) if root is not None else IIO_ROOT
        self._open_device = open_device or _default_open_device
        self.mount: tuple[float, ...] | None = None
        if mount is not None:
            values = tuple(float(value) for value in mount)
            if len(values) != 9 or not all(math.isfinite(v) for v in values):
                raise ValueError("mount matrix must be 9 finite numbers")
            self.mount = values
        self.device: Path | None = None
        self.fd: int | None = None
        self.axes: tuple[str, ...] = _AXES
        self.types: tuple[SampleType, ...] = ()
        self.scale = 1.0
        self.offset = 0.0
        self.saved: dict[str, str] = {}

    # -- lifecycle ---------------------------------------------------------

    def setup(self) -> None:
        """Find the device, configure the buffer and open the character node."""
        try:
            self._setup()
        except Unavailable:
            self.close()
            raise
        except (OSError, ValueError) as exc:
            self.close()
            raise Unavailable(f"cannot start IIO buffer: {exc}") from exc

    def _setup(self) -> None:
        device = _find_device(self.root, self.name)
        index = device.name.removeprefix(DEVICE_PREFIX)
        if not index.isdigit():
            raise Unavailable(f"unexpected IIO device directory {device.name!r}")
        trigger_name = f"{self.name}-dev{index}"
        if _find_trigger(self.root, trigger_name) is None:
            raise Unavailable(f"IIO trigger {trigger_name} not found")

        self.device = device
        self.axes, self.types = scan_plan(device)
        scale_text = _optional_text(device / "in_accel_scale")
        if not scale_text:
            raise Unavailable("IIO device does not expose in_accel_scale")
        scale = float(scale_text)
        if not math.isfinite(scale) or scale == 0.0:
            raise Unavailable(f"invalid IIO scale {scale_text!r}")
        self.scale = scale
        offset_text = _optional_text(device / "in_accel_offset")
        offset = float(offset_text) if offset_text else 0.0
        if not math.isfinite(offset):
            raise Unavailable(f"invalid IIO offset {offset_text!r}")
        self.offset = offset
        if self.mount is None:
            self.mount = read_iio_mount(device)

        # Never steal a buffer that somebody else already owns.
        if _optional_text(device / "buffer" / "enable") == "1":
            raise Unavailable(f"{self.name} buffer is already enabled")

        self.saved = {
            "buffer/length": _optional_text(device / "buffer" / "length"),
            "buffer/watermark": _optional_text(device / "buffer" / "watermark"),
            "trigger/current_trigger": _optional_text(
                device / "trigger" / "current_trigger"
            ),
            "timestamp": _optional_text(
                device / "scan_elements" / "in_timestamp_en"
            ),
            "x": _optional_text(device / "scan_elements" / "in_accel_x_en"),
            "y": _optional_text(device / "scan_elements" / "in_accel_y_en"),
            "z": _optional_text(device / "scan_elements" / "in_accel_z_en"),
        }

        for axis in _AXES:
            _write_text(device / "scan_elements" / f"in_accel_{axis}_en", 1)
        _write_text(device / "scan_elements" / "in_timestamp_en", 0)
        _write_text(device / "trigger" / "current_trigger", trigger_name)
        _write_text(device / "buffer" / "length", 64)
        _write_text(device / "buffer" / "watermark", 1)
        _write_text(device / "buffer" / "enable", 1)
        self.fd = self._open_device(f"/dev/iio:device{index}")

    def __enter__(self) -> Self:
        self.setup()
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        """Close the character node and restore the previous sysfs state."""
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        device, self.device = self.device, None
        saved, self.saved = self.saved, {}
        if device is None or not saved:
            return
        try:
            _write_text(device / "buffer" / "enable", 0)
            if saved.get("trigger/current_trigger") is not None:
                _write_text(
                    device / "trigger" / "current_trigger",
                    saved["trigger/current_trigger"],
                )
            if saved.get("buffer/length"):
                _write_text(device / "buffer" / "length", saved["buffer/length"])
            if saved.get("buffer/watermark"):
                _write_text(
                    device / "buffer" / "watermark", saved["buffer/watermark"]
                )
            for axis in _AXES:
                if saved.get(axis) is not None:
                    _write_text(
                        device / "scan_elements" / f"in_accel_{axis}_en",
                        saved[axis],
                    )
            if saved.get("timestamp") is not None:
                _write_text(
                    device / "scan_elements" / "in_timestamp_en",
                    saved["timestamp"],
                )
        except OSError:
            # The device may have been unplugged; there is nothing to restore.
            pass

    # -- sampling ----------------------------------------------------------

    def __iter__(self) -> Iterator[tuple[float, ...]]:
        if self.fd is None:
            raise Unavailable("IIO buffer is not set up")
        sample_size = sum(kind.size for kind in self.types)
        last_sample = time.monotonic()
        try:
            while not self.stop.is_set():
                try:
                    ready, _, _ = select.select([self.fd], [], [], SAMPLE_WAIT_SECONDS)
                except (OSError, ValueError) as exc:
                    raise Unavailable(f"IIO select failed: {exc}") from exc
                if self.stop.is_set():
                    break
                if not ready:
                    # Some sensor-hub firmware accepts the buffer setup but
                    # never delivers samples.  Permanently waiting would leave
                    # the service without a pose; fall back to sysfs instead.
                    if time.monotonic() - last_sample >= IIO_NO_SAMPLE_SECONDS:
                        raise Unavailable(f"{self.name} produced no samples")
                    continue
                data = os.read(self.fd, sample_size)
                if len(data) != sample_size:
                    raise Unavailable(
                        f"{self.name} returned a {len(data)} byte sample, "
                        f"expected {sample_size}"
                    )
                last_sample = time.monotonic()
                values = decode_sample(data, self.types)
                by_axis = dict(zip(self.axes, values, strict=True))
                raw = [
                    (by_axis[axis] + self.offset) * self.scale for axis in _AXES
                ]
                vector = apply_mount(raw, self.mount)
                yield tuple(float(value) for value in vector)
        finally:
            self.close()


def iio_devices(root: str | Path | None = None) -> list[dict[str, object]]:
    """List IIO devices from sysfs (read only; used by the probe)."""
    base = Path(root) if root is not None else IIO_ROOT
    try:
        entries = sorted(base.glob(f"{DEVICE_PREFIX}*"))
    except OSError:
        return []
    devices: list[dict[str, object]] = []
    for device in entries:
        name = _optional_text(device / "name")
        if not name:
            continue
        scan_types: dict[str, str] = {}
        scan_indices: dict[str, str] = {}
        for axis in _AXES:
            type_text = _optional_text(
                device / "scan_elements" / f"in_accel_{axis}_type"
            )
            if type_text:
                scan_types[axis] = type_text
            index_text = _optional_text(
                device / "scan_elements" / f"in_accel_{axis}_index"
            )
            if index_text:
                scan_indices[axis] = index_text
        devices.append(
            {
                "device": str(device),
                "name": name,
                "scan_types": scan_types,
                "scan_indices": scan_indices,
                "scale": _optional_text(device / "in_accel_scale"),
                "offset": _optional_text(device / "in_accel_offset"),
                "sampling_frequency": _optional_text(
                    device / "in_accel_sampling_frequency"
                ),
                "mount_matrix": list(read_iio_mount(device) or []),
                "trigger": _optional_text(device / "trigger" / "current_trigger"),
                "buffer_enabled": _optional_text(device / "buffer" / "enable"),
            }
        )
    return devices
