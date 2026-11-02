"""Read-only sysfs access for input devices, capabilities and the tablet switch.

The kernel prints capability bitmaps as space separated hex *words*, most
significant word first.  Each word is ``sizeof(unsigned long) * 8`` bits wide,
so a 32-bit user space sees 32-bit words even on a 64-bit kernel bitmap.  The
parser is strict: a malformed bitmap raises :class:`ValueError` instead of
silently becoming 0, because "no EV_KEY" is a security-relevant input to the
relay discovery rules (fail-closed).
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
import struct
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..model import HardwareStatus
from ..protocol import RELAY_VIRTUAL_NAME, RELAY_VIRTUAL_PHYS

_LOG = logging.getLogger(__name__)

INPUT_ROOT = Path("/sys/class/input")
DEV_INPUT_ROOT = Path("/dev/input")

#: A cached switch candidate is re-enumerated at this interval so hotplug and
#: ambiguity are still detected, while the 100 ms service tick only performs
#: the cheap identity + EVIOCGSW read against the already discovered node.
SWITCH_RESCAN_SECONDS = 1.0

# Input event type/code constants (linux/input-event-codes.h).
EV_SYN = 0x00
EV_KEY = 0x01
EV_ABS = 0x03
EV_SW = 0x05
SW_TABLET_MODE = 0x01
KEY_A = 30
KEY_Z = 44
ABS_MT_POSITION_X = 0x35
INPUT_PROP_POINTER = 0x00
INPUT_PROP_DIRECT = 0x01
INPUT_PROP_BUTTONPAD = 0x05
BUS_USB = 0x03
BUS_BLUETOOTH = 0x05

CAPABILITY_FILES = ("ev", "key", "rel", "abs", "msc", "sw", "led", "snd", "ff")

#: Kernel bitmap word width used by the sysfs printers.  The kernel formats
#: capability bitmaps with its own ``BITS_PER_LONG``; on the supported x86_64
#: target that equals the user-space pointer width, so deriving it from the
#: pointer size is correct here.  A 32-bit user space on a 64-bit kernel would
#: need the kernel word width instead (not a supported configuration).
WORD_BITS = struct.calcsize("P") * 8
WORD_BYTES = WORD_BITS // 8


def _ioc(direction: int, ioctl_type: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ioctl_type << 8) | number


_IOC_READ = 2

# EVIOCGNAME/EVIOCGPHYS take the buffer length; EVIOCGSW takes the state size.
EVIOCGNAME = _ioc(_IOC_READ, ord("E"), 0x06, 256)
EVIOCGPHYS = _ioc(_IOC_READ, ord("E"), 0x07, 256)
EVIOCGSW = _ioc(_IOC_READ, ord("E"), 0x1B, WORD_BYTES)


class _IdentityMismatch(Exception):
    """The opened node no longer matches the device found in sysfs."""


class _CandidateError(Exception):
    """A device claims the tablet switch but its capabilities are unverifiable.

    Treated as a read error (not as "absent"): fail closed, because silently
    ignoring it could enable software tablet modes while a physical switch is
    present.
    """


def read_text(path: str | Path) -> str:
    """Read a sysfs text attribute with undecodable bytes replaced."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def read_bitmap(text: str, *, word_bits: int | None = None) -> int:
    """Parse a kernel bitmap string into an integer.

    ``word_bits`` defaults to the platform pointer width; it only exists so
    tests can exercise 32-bit user space behaviour on a 64-bit machine.
    """
    bits = WORD_BITS if word_bits is None else int(word_bits)
    if bits <= 0 or bits % 8 != 0:
        raise ValueError(f"invalid bitmap word width {bits}")
    words = (text or "").split()
    if not words:
        raise ValueError("empty kernel bitmap")
    max_digits = bits // 4
    value = 0
    for word in words:
        if len(word) > max_digits:
            raise ValueError(f"bitmap word {word!r} exceeds {bits} bits")
        try:
            parsed = int(word, 16)
        except ValueError as exc:
            raise ValueError(f"invalid bitmap word {word!r}") from exc
        if parsed < 0 or parsed >= 1 << bits:
            raise ValueError(f"bitmap word {word!r} out of range")
        value = (value << bits) | parsed
    return value


def read_capabilities(path: str | Path) -> dict[str, int]:
    """Read ``capabilities/{ev,key,...}`` under an event or device directory.

    Missing files mean the device does not expose that capability and are
    omitted from the result; unreadable or malformed files raise
    :class:`OSError`/:class:`ValueError` so callers can fail closed.
    """
    base = Path(path)
    directory = base / "capabilities"
    if not directory.is_dir():
        directory = base / "device" / "capabilities"
    if not directory.is_dir():
        raise OSError(f"no capabilities directory under {base}")
    result: dict[str, int] = {}
    for name in CAPABILITY_FILES:
        file = directory / name
        try:
            text = read_text(file)
        except FileNotFoundError:
            continue
        # PermissionError/other OSError propagate unchanged so callers can
        # distinguish "permission" from "read_error" and fail closed.
        result[name] = read_bitmap(text)
    return result


def _features(
    capabilities: dict[str, int], properties: int, bustype: int | None
) -> dict[str, bool]:
    event_types = capabilities.get("ev", 0)
    switches = capabilities.get("sw", 0)
    keys = capabilities.get("key", 0)
    absolute = capabilities.get("abs", 0)
    return {
        "event_switch": bool(event_types & (1 << EV_SW)),
        "event_key": bool(event_types & (1 << EV_KEY)),
        "event_abs": bool(event_types & (1 << EV_ABS)),
        "tablet_switch": bool(switches & (1 << SW_TABLET_MODE)),
        "switch_only": (
            bool(event_types & (1 << EV_SW))
            and keys == 0
            and switches == (1 << SW_TABLET_MODE)
        ),
        "keyboard_keys": bool(keys & (1 << KEY_A)) and bool(keys & (1 << KEY_Z)),
        "multitouch": bool(absolute & (1 << ABS_MT_POSITION_X)),
        "direct": bool(properties & (1 << INPUT_PROP_DIRECT)),
        "pointer": bool(properties & (1 << INPUT_PROP_POINTER)),
        "buttonpad": bool(properties & (1 << INPUT_PROP_BUTTONPAD)),
        "external_bus": bustype in (BUS_USB, BUS_BLUETOOTH),
    }


def _optional_text(path: Path) -> str:
    try:
        return read_text(path).strip()
    except OSError:
        return ""


def _hex_value(text: str) -> int | None:
    if not text:
        return None
    try:
        return int(text, 16)
    except ValueError:
        return None


def input_device_info(event_dir: str | Path) -> dict[str, Any] | None:
    """Read one ``/sys/class/input/eventN`` entry; best effort, read only."""
    event_dir = Path(event_dir)
    device_dir = event_dir / "device"
    try:
        name = read_text(device_dir / "name").strip()
    except OSError:
        return None
    if not name:
        return None

    phys = _optional_text(device_dir / "phys")
    bustype_text = _optional_text(device_dir / "id" / "bustype")
    vendor_text = _optional_text(device_dir / "id" / "vendor")
    product_text = _optional_text(device_dir / "id" / "product")
    bustype = _hex_value(bustype_text)
    virtual = name == RELAY_VIRTUAL_NAME or (bool(phys) and phys == RELAY_VIRTUAL_PHYS)

    capabilities: dict[str, int] = {}
    capabilities_error: str | None = None
    try:
        capabilities = read_capabilities(device_dir)
    except (OSError, ValueError) as exc:
        capabilities_error = str(exc)
        _LOG.debug("cannot read capabilities of %s: %s", event_dir, exc)

    properties = 0
    properties_error: str | None = None
    try:
        properties = read_bitmap(read_text(device_dir / "properties"))
    except (OSError, ValueError) as exc:
        properties_error = str(exc)
        _LOG.debug("cannot read properties of %s: %s", event_dir, exc)

    entry: dict[str, Any] = {
        "node": f"/dev/input/{event_dir.name}",
        "sysfs": str(event_dir),
        "name": name,
        "phys": phys,
        "bustype": bustype_text,
        "bustype_value": bustype,
        "vendor": vendor_text,
        "product": product_text,
        "virtual": virtual,
        "capabilities": capabilities,
        # Raw capability bitmaps (also exposed under their historical names so
        # probe reports and docs/hardware-profiles.md stay in sync).
        "switches": capabilities.get("sw", 0),
        "keys": capabilities.get("key", 0),
        "abs": capabilities.get("abs", 0),
        "props": properties,
        "properties": properties,
        "features": _features(capabilities, properties, bustype),
    }
    if capabilities_error is not None:
        entry["capabilities_error"] = capabilities_error
    if properties_error is not None:
        entry["properties_error"] = properties_error
    return entry


def input_devices(sysfs_root: str | Path | None = None) -> list[dict[str, Any]]:
    """List input devices from sysfs (never opens /dev/input nodes)."""
    root = Path(sysfs_root) if sysfs_root is not None else INPUT_ROOT
    try:
        entries = sorted(root.glob("event*"))
    except OSError:
        return []
    devices: list[dict[str, Any]] = []
    for event_dir in entries:
        info = input_device_info(event_dir)
        if info is not None:
            devices.append(info)
    return devices


def _switch_candidate(event_dir: Path, switch_name: str | None) -> dict[str, Any] | None:
    """Return a candidate description if the sysfs entry is a safe switch."""
    device_dir = event_dir / "device"
    try:
        name = read_text(device_dir / "name").strip()
    except FileNotFoundError:
        return None
    if not name:
        return None
    if switch_name is not None and name != switch_name:
        return None
    phys = _optional_text(device_dir / "phys")
    if name == RELAY_VIRTUAL_NAME or (bool(phys) and phys == RELAY_VIRTUAL_PHYS):
        return None

    capabilities = read_capabilities(device_dir)
    switches = capabilities.get("sw")
    if switches is None or not switches & (1 << SW_TABLET_MODE):
        return None  # not a tablet-switch-like device; other switches ignored
    # From here on the device claims the tablet switch bit.  Require complete,
    # safe evidence; otherwise fail closed instead of reporting "absent".
    event_types = capabilities.get("ev")
    keys = capabilities.get("key")
    if event_types is None or keys is None:
        raise _CandidateError(
            "tablet switch capabilities are incomplete (ev/key missing)"
        )
    # Mirror backend/switch.c exactly: a device that declares EV_KEY events is
    # not a switch-only aggregation source, even when its key bitmap is empty.
    if event_types & (1 << EV_KEY):
        return None
    if keys != 0:
        return None
    if not event_types & (1 << EV_SW):
        return None
    if switches != (1 << SW_TABLET_MODE):
        return None
    return {"event": event_dir.name, "name": name, "phys": phys}


def _default_open(path: str) -> int:
    return os.open(
        path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW
    )


def _ioctl_string(
    ioctl_call: Callable[..., Any],
    descriptor: int,
    request: int,
    size: int = 256,
) -> str:
    buffer = bytearray(size)
    ioctl_call(descriptor, request, buffer, True)
    return bytes(buffer).split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def _read_state(
    dev_root: Path,
    candidate: dict[str, Any],
    open_device: Callable[[str], int],
    ioctl_device: Callable[..., Any],
) -> bool:
    """Open the candidate and read EVIOCGSW, verifying the device identity."""
    descriptor = open_device(str(dev_root / candidate["event"]))
    try:
        opened_name = _ioctl_string(ioctl_device, descriptor, EVIOCGNAME)
        if opened_name != candidate["name"]:
            raise _IdentityMismatch("device name changed after enumeration")
        try:
            opened_phys = _ioctl_string(ioctl_device, descriptor, EVIOCGPHYS)
        except OSError as exc:
            # Many input devices (including Intel HID switches on this
            # hardware) have no phys; the kernel reports ENOENT for
            # EVIOCGPHYS.  That is not a missing device node.
            if exc.errno == errno.ENOENT:
                opened_phys = ""
            else:
                raise
        if candidate["phys"] and opened_phys and opened_phys != candidate["phys"]:
            raise _IdentityMismatch("device phys changed after enumeration")
        state = bytearray(WORD_BYTES)
        ioctl_device(descriptor, EVIOCGSW, state, True)
        return bool(state[0] & (1 << SW_TABLET_MODE))
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _scan_candidates(
    root: Path, switch_name: str | None
) -> tuple[list[dict[str, Any]], int, HardwareStatus | None]:
    """Enumerate safe switch candidates without opening device nodes."""
    try:
        entries = sorted(root.glob("event*"))
    except OSError as exc:
        _LOG.warning("cannot enumerate %s: %s", root, exc)
        return [], 0, HardwareStatus.READ_ERROR

    candidates: list[dict[str, Any]] = []
    errors = 0
    for event_dir in entries:
        try:
            candidate = _switch_candidate(event_dir, switch_name)
        except PermissionError:
            _LOG.warning("permission denied reading %s", event_dir)
            return [], 0, HardwareStatus.PERMISSION
        except _CandidateError as exc:
            errors += 1
            _LOG.warning("cannot verify %s: %s", event_dir, exc)
            continue
        except (OSError, ValueError) as exc:
            errors += 1
            _LOG.warning("cannot inspect %s: %s", event_dir, exc)
            continue
        if candidate is not None:
            candidates.append(candidate)
    return candidates, errors, None


def _read_candidates(
    dev: Path,
    candidates: list[dict[str, Any]],
    opener: Callable[[str], int],
    ioctl_call: Callable[..., Any],
) -> tuple[list[tuple[dict[str, Any], bool]], int, HardwareStatus | None]:
    """Read every candidate, returning readable matches and soft errors."""
    matches: list[tuple[dict[str, Any], bool]] = []
    errors = 0
    for candidate in candidates:
        try:
            matches.append(
                (candidate, _read_state(dev, candidate, opener, ioctl_call))
            )
        except PermissionError:
            _LOG.warning("permission denied opening %s", candidate["event"])
            return [], 0, HardwareStatus.PERMISSION
        except _IdentityMismatch as exc:
            errors += 1
            _LOG.warning("ignoring changed device %s: %s", candidate["event"], exc)
        except FileNotFoundError:
            # A candidate existed in sysfs but its node is missing: fail
            # closed instead of claiming "no hardware".
            errors += 1
            _LOG.debug("%s node is missing", candidate["event"])
        except (OSError, ValueError) as exc:
            errors += 1
            _LOG.warning("cannot read %s: %s", candidate["event"], exc)
    return matches, errors, None


def _discover_and_read(
    root: Path,
    dev: Path,
    switch_name: str | None,
    opener: Callable[[str], int],
    ioctl_call: Callable[..., Any],
) -> tuple[
    list[dict[str, Any]],
    list[tuple[dict[str, Any], bool]],
    int,
    HardwareStatus | None,
]:
    """Run full discovery and read all candidates through one code path."""
    candidates, errors, failure = _scan_candidates(root, switch_name)
    if failure is not None:
        return [], [], errors, failure
    matches, read_errors, failure = _read_candidates(
        dev, candidates, opener, ioctl_call
    )
    return candidates, matches, errors + read_errors, failure


def _combine_switch_result(
    matches: list[tuple[dict[str, Any], bool]], errors: int
) -> tuple[bool | None, HardwareStatus]:
    if len(matches) > 1:
        _LOG.warning("multiple tablet-mode switches found; refusing to guess")
        return None, HardwareStatus.AMBIGUOUS
    if len(matches) == 1:
        return matches[0][1], HardwareStatus.OK
    if errors:
        return None, HardwareStatus.READ_ERROR
    # Drivers such as intel-hid publish the switch only after the first event;
    # until then software tablet modes are still usable.
    return False, HardwareStatus.ABSENT


def query_switch(
    switch_name: str | None = None,
    *,
    sysfs_root: str | Path | None = None,
    dev_root: str | Path | None = None,
    open_device: Callable[[str], int] | None = None,
    ioctl_device: Callable[..., Any] | None = None,
) -> tuple[bool | None, HardwareStatus]:
    """Read the physical switch-only ``SW_TABLET_MODE`` device.

    Returns ``(state, status)`` where ``state`` is ``True``/``False`` for a
    usable hardware source, and ``None`` for permission/read/ambiguity errors
    (never ``False``, so callers do not enable software tablet modes by
    mistake).  The relay's own virtual switch is always excluded.
    """
    root = Path(sysfs_root) if sysfs_root is not None else INPUT_ROOT
    dev = Path(dev_root) if dev_root is not None else DEV_INPUT_ROOT
    opener = open_device if open_device is not None else _default_open
    ioctl_call = ioctl_device if ioctl_device is not None else fcntl.ioctl

    _candidates, matches, errors, failure = _discover_and_read(
        root, dev, switch_name, opener, ioctl_call
    )
    if failure is not None:
        return None, failure
    return _combine_switch_result(matches, errors)


class SwitchReader:
    """Poll the tablet switch without rescanning every input device per tick.

    The first call and every ``rescan_seconds`` perform the strict full
    discovery used by :func:`query_switch`.  Intermediate calls reuse the one
    verified candidate and only re-open that event node, verify its identity,
    and read ``EVIOCGSW``.  A failed cached read invalidates the cache so the
    next call discovers a disappeared or replaced device instead of returning
    stale state.
    """

    def __init__(
        self,
        switch_name: str | None = None,
        *,
        sysfs_root: str | Path | None = None,
        dev_root: str | Path | None = None,
        open_device: Callable[[str], int] | None = None,
        ioctl_device: Callable[..., Any] | None = None,
        rescan_seconds: float = SWITCH_RESCAN_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = Path(sysfs_root) if sysfs_root is not None else INPUT_ROOT
        self._dev = Path(dev_root) if dev_root is not None else DEV_INPUT_ROOT
        self._switch_name = switch_name
        self._opener = open_device if open_device is not None else _default_open
        self._ioctl = ioctl_device if ioctl_device is not None else fcntl.ioctl
        self._rescan_seconds = max(0.0, float(rescan_seconds))
        self._monotonic = monotonic
        self._candidate: dict[str, Any] | None = None
        self._cached_result: tuple[bool | None, HardwareStatus] = (
            False,
            HardwareStatus.ABSENT,
        )
        self._next_scan = float("-inf")

    @property
    def candidate(self) -> str | None:
        candidate = self._candidate
        return None if candidate is None else str(candidate["event"])

    def invalidate(self) -> None:
        """Discard the cached node (for suspend/resume or explicit recovery)."""
        self._candidate = None
        self._next_scan = float("-inf")
        self._cached_result = (False, HardwareStatus.ABSENT)

    def read(self, now: float | None = None) -> tuple[bool | None, HardwareStatus]:
        current = self._monotonic() if now is None else float(now)
        candidate = self._candidate
        if candidate is not None and current < self._next_scan:
            try:
                value = _read_state(
                    self._dev, candidate, self._opener, self._ioctl
                )
            except PermissionError:
                self.invalidate()
                return None, HardwareStatus.PERMISSION
            except _IdentityMismatch:
                self.invalidate()
                return None, HardwareStatus.READ_ERROR
            except FileNotFoundError:
                self.invalidate()
                return None, HardwareStatus.READ_ERROR
            except (OSError, ValueError):
                self.invalidate()
                return None, HardwareStatus.READ_ERROR
            return value, HardwareStatus.OK
        if current < self._next_scan:
            return self._cached_result

        candidates, matches, errors, failure = _discover_and_read(
            self._root,
            self._dev,
            self._switch_name,
            self._opener,
            self._ioctl,
        )
        if failure is not None:
            result: tuple[bool | None, HardwareStatus] = (None, failure)
            self._candidate = None
        else:
            result = _combine_switch_result(matches, errors)
            self._candidate = (
                candidates[0] if len(candidates) == 1 and len(matches) == 1 else None
            )
        self._cached_result = result
        self._next_scan = current + self._rescan_seconds
        return result
