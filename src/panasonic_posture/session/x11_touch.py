"""X11 touchscreen mapping for the RandR display backend.

X11 does not automatically keep absolute input devices attached to a rotated
RandR output.  The X11 display transaction therefore snapshots the devices'
``Coordinate Transformation Matrix`` properties before rotating, maps the
built-in touch devices to the panel with ``xinput map-to-output``, and restores
the exact original matrices on exit or failure.

Device discovery deliberately uses the kernel ``INPUT_PROP_DIRECT`` property
instead of parsing localised ``xinput list`` output.  Direct devices sharing a
physical path (a touchscreen and its stylus interface, for example) form one
panel; if more than one unrelated direct device is present, automatic mapping
is disabled because the built-in panel cannot be identified safely.
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from ..hardware import sysfs
from ..protocol import ErrorCode
from .errors import DisplayError

log = logging.getLogger(__name__)

TOUCH_DEVICE_ENV = "PANASONIC_POSTURE_TOUCH_DEVICE"
TOUCH_MAPPING_KIND = "x11-touch-mapping"
CTM_PROPERTY = "Coordinate Transformation Matrix"
COMMAND_TIMEOUT_SECONDS = 2.5

_CTM_LINE = re.compile(
    r"^\s*" + re.escape(CTM_PROPERTY) + r"\s+\(\d+\):\s*(.+?)\s*$"
)
_PHYS_INPUT_SUFFIX = re.compile(r"/input\d+$")


def parse_ctm(text: str) -> tuple[str, ...] | None:
    """Return the nine values of the first valid matrix in ``xinput list-props``."""

    for line in str(text or "").splitlines():
        match = _CTM_LINE.match(line)
        if match is None:
            continue
        values = tuple(value.strip() for value in match.group(1).split(","))
        if len(values) != 9 or any(not value for value in values):
            return None
        try:
            numbers = tuple(float(value) for value in values)
        except ValueError:
            return None
        if not all(math.isfinite(value) for value in numbers):
            return None
        return values
    return None


def parse_device_names(text: str) -> tuple[str, ...]:
    """Parse ``xinput list --name-only`` without depending on list formatting."""

    return tuple(line.strip() for line in str(text or "").splitlines() if line.strip())


def _forced_devices(environ: Mapping[str, str]) -> tuple[str, ...]:
    raw = str(environ.get(TOUCH_DEVICE_ENV, ""))
    return tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))


def _direct_devices(devices: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for entry in devices:
        features = entry.get("features")
        name = entry.get("name")
        if (
            isinstance(features, Mapping)
            and features.get("direct")
            and isinstance(name, str)
            and name
        ):
            result.append(dict(entry))
    return result


def _group_key(entry: Mapping[str, Any]) -> str:
    phys = entry.get("phys")
    if isinstance(phys, str) and phys:
        return f"phys:{_PHYS_INPUT_SUFFIX.sub('', phys)}"
    name = str(entry.get("name") or "").lower()
    if name.endswith(" stylus"):
        name = name[: -len(" stylus")]
    return f"name:{name}"


def select_touch_devices(
    devices: Iterable[Mapping[str, Any]],
    available_names: Iterable[str],
    forced: Sequence[str] = (),
) -> tuple[str, ...]:
    """Select direct devices belonging to the built-in panel.

    An explicit list in ``PANASONIC_POSTURE_TOUCH_DEVICE`` wins.  Otherwise all
    direct devices from one physical path are selected; several unrelated paths
    are ambiguous, so an empty selection leaves touch mapping disabled rather
    than guessing and moving an external touchscreen to the internal panel.
    """

    available = set(available_names)
    direct = _direct_devices(devices)
    if forced:
        return tuple(name for name in forced if name in available)

    eligible = [entry for entry in direct if str(entry.get("name")) in available]
    groups: dict[str, list[dict[str, Any]]] = {}
    for entry in eligible:
        groups.setdefault(_group_key(entry), []).append(entry)
    if len(groups) != 1:
        return ()
    return tuple(
        sorted(str(entry["name"]) for entry in next(iter(groups.values())))
    )


def _snapshot_devices(snapshot: Mapping[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if snapshot.get("kind") != TOUCH_MAPPING_KIND:
        raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "invalid-touch-mapping-state")
    raw_devices = snapshot.get("devices")
    if not isinstance(raw_devices, list) or not raw_devices:
        raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "invalid-touch-mapping-state")
    result = []
    for item in raw_devices:
        if not isinstance(item, dict):
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "invalid-touch-mapping-state")
        name = item.get("name")
        matrix = item.get("matrix")
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(matrix, (list, tuple))
            or len(matrix) != 9
            or not all(isinstance(value, str) and value for value in matrix)
        ):
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "invalid-touch-mapping-state")
        try:
            numbers = tuple(float(value) for value in matrix)
        except ValueError:
            raise DisplayError(
                ErrorCode.DISPLAY_TRANSFORM, "invalid-touch-mapping-state"
            ) from None
        if not all(math.isfinite(value) for value in numbers):
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "invalid-touch-mapping-state")
        result.append((name, tuple(matrix)))
    return tuple(result)


def _run_command(args: list[str], timeout: float):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class X11TouchMapping:
    """Snapshot, map and restore X11 direct-input coordinate matrices."""

    def __init__(
        self,
        runner: Callable[[list[str], float], object] | None = None,
        *,
        which: Callable[[str], str | None] = shutil.which,
        devices: Callable[[], Iterable[Mapping[str, Any]]] = sysfs.input_devices,
        environ: Mapping[str, str] | None = None,
    ):
        environment = os.environ if environ is None else environ
        self._runner = runner or _run_command
        self._binary = which("xinput")
        self._devices = devices
        self._forced = _forced_devices(environment)
        self._notices: set[str] = set()

    def _notice(self, key: str, message: str, *args: object) -> None:
        if key in self._notices:
            return
        self._notices.add(key)
        log.warning(message, *args)

    def capture(self) -> dict | None:
        """Snapshot direct touch devices, or return ``None`` when unavailable."""

        try:
            entries = _direct_devices(self._devices())
        except OSError as error:
            self._notice("sysfs", "cannot inspect X11 touch devices: %s", error)
            return None
        if not entries:
            return None
        if not self._binary:
            self._notice(
                "missing",
                "xinput is not installed; touchscreen mapping follows the desktop only",
            )
            return None

        try:
            names = parse_device_names(
                self._run(
                    [self._binary, "list", "--name-only"],
                    ErrorCode.DISPLAY_UNAVAILABLE,
                )
            )
            selected = select_touch_devices(entries, names, self._forced)
            if not selected:
                self._notice(
                    "selection",
                    "cannot identify a single built-in X11 touchscreen; "
                    "set %s to its xinput device name",
                    TOUCH_DEVICE_ENV,
                )
                return None

            devices = []
            for name in selected:
                properties = self._run(
                    [self._binary, "list-props", name], ErrorCode.DISPLAY_UNAVAILABLE
                )
                matrix = parse_ctm(properties)
                if matrix is None:
                    self._notice(
                        ("matrix", name),
                        "X11 touch device %r has no usable coordinate matrix; skipping it",
                        name,
                    )
                    continue
                devices.append({"name": name, "matrix": list(matrix)})
        except DisplayError as error:
            self._notice(
                ("query", str(error)),
                "cannot inspect the X11 touch mapping; leaving it unchanged: %s",
                error,
            )
            return None
        if not devices:
            return None
        return {"kind": TOUCH_MAPPING_KIND, "devices": devices}

    def map_to_output(self, snapshot: Mapping[str, Any], output: str) -> None:
        """Map every snapshotted device to *output* at its current rotation."""

        if not self._binary:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "xinput-not-found")
        failure: DisplayError | None = None
        for name, _matrix in _snapshot_devices(snapshot):
            try:
                self._run(
                    [self._binary, "map-to-output", name, output],
                    ErrorCode.DISPLAY_TRANSFORM,
                )
            except DisplayError as error:
                failure = failure or error
        if failure is not None:
            raise failure

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        """Restore the exact coordinate matrices captured before the rotation."""

        if not self._binary:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "xinput-not-found")
        failure: DisplayError | None = None
        for name, matrix in _snapshot_devices(snapshot):
            try:
                self._run(
                    [self._binary, "set-prop", name, CTM_PROPERTY, *matrix],
                    ErrorCode.DISPLAY_TRANSFORM,
                )
            except DisplayError as error:
                failure = failure or error
        if failure is not None:
            raise failure

    def _run(self, args: list[str], code: str) -> str:
        try:
            result = self._runner(args, timeout=COMMAND_TIMEOUT_SECONDS)
        except FileNotFoundError as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "xinput-not-found") from error
        except subprocess.TimeoutExpired as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "xinput-timeout") from error
        except OSError as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, str(error)) from error
        if int(getattr(result, "returncode", 1)) != 0:
            detail = str(getattr(result, "stderr", "") or "").strip()
            raise DisplayError(code, detail[:200] or "xinput-command-failed")
        return str(getattr(result, "stdout", "") or "")


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "CTM_PROPERTY",
    "TOUCH_DEVICE_ENV",
    "TOUCH_MAPPING_KIND",
    "X11TouchMapping",
    "parse_ctm",
    "parse_device_names",
    "select_touch_devices",
]
