"""KDE Plasma 6 display backend.

Plasma's output configuration is exposed by two command-line tools in the
Plasma 6 series:

* ``kscreenctl`` from kscreen 6.8 and newer;
* ``kscreen-doctor`` from libkscreen 6.0 through the 6.8 series.

Both support explicit rotation and an automatic-rotation policy
(``never`` / ``in-tablet-mode`` / ``always``).  The policy is Plasma's
equivalent of GNOME's touchscreen orientation lock.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .errors import DisplayError, ErrorCode
from .journal import Journal
from .transaction import DisplayState, SoftwareDisplay

COMMAND_TIMEOUT_SECONDS = 2.5
READBACK_DELAY_SECONDS = 0.15

_INTERNAL_NAME = re.compile(r"^(?:edp|ldp|lvds|dsi)-?\d*$", re.IGNORECASE)
_DOCTOR_ROTATIONS = {0: "normal", 1: "left", 2: "inverted", 3: "right"}
_CTL_ROTATIONS = {0: "normal", 1: "90", 2: "180", 3: "270"}
_DOCTOR_POLICIES = {"never": "never", "in-tablet-mode": "inTabletMode", "always": "always"}
_CTL_POLICIES = {"never": "never", "in-tablet-mode": "in-tablet-mode", "always": "always"}


def run_command(args: list[str], timeout: float):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@dataclass(frozen=True)
class PlasmaOutput:
    name: str
    transform: int
    auto_rotate: str | None
    auto_rotate_supported: bool


def _transform_from_value(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        # KScreen uses the RandR bit flags for rotation values.
        return {1: 0, 2: 1, 4: 2, 8: 3}.get(value)
    if isinstance(value, str):
        return {
            "none": 0,
            "normal": 0,
            "0": 0,
            "left": 1,
            "90": 1,
            "inverted": 2,
            "180": 2,
            "right": 3,
            "270": 3,
        }.get(value.strip().lower())
    return None


def _policy_from_value(value) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return {0: "never", 1: "in-tablet-mode", 2: "always"}.get(value)
    if isinstance(value, str):
        normalized = value.strip().lower().replace("_", "-")
        if normalized == "intabletmode":
            normalized = "in-tablet-mode"
        if normalized in _DOCTOR_POLICIES:
            return normalized
    return None


def _capabilities(item: dict) -> set[str]:
    raw = item.get("capabilities")
    if isinstance(raw, list):
        return {str(value) for value in raw}
    if isinstance(raw, dict):
        return {str(key) for key, value in raw.items() if value}
    return set()


def _pick_output(outputs: list[PlasmaOutput], forced_output: str | None) -> PlasmaOutput:
    if forced_output:
        matches = [output for output in outputs if output.name == forced_output]
        if len(matches) == 1:
            return matches[0]
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "requested-output-not-active")

    internal = [output for output in outputs if _INTERNAL_NAME.match(output.name)]
    if len(internal) == 1:
        return internal[0]
    if len(internal) > 1:
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-unique")
    if len(outputs) == 1:
        return outputs[0]
    raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-found")


def _parse_outputs(text: str) -> list[dict]:
    try:
        document = json.loads(str(text or ""))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "invalid-kscreen-json") from error
    if isinstance(document, dict):
        items = document.get("outputs")
        if items is None and "name" in document:
            items = [document]
    else:
        items = document
    if not isinstance(items, list):
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "invalid-kscreen-output-list")
    return [item for item in items if isinstance(item, dict)]


def _select_output(
    text: str,
    forced_output: str | None,
    *,
    builtin_types: Callable[[dict], bool],
) -> PlasmaOutput:
    active: list[tuple[PlasmaOutput, dict]] = []
    for item in _parse_outputs(text):
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        if item.get("connected", True) is False or item.get("enabled", True) is False:
            continue
        transform = _transform_from_value(item.get("rotation"))
        if transform is None:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "current-rotation-unavailable")
        capabilities = _capabilities(item)
        capabilities_present = "capabilities" in item
        raw_policy = item.get("autoRotate", item.get("autoRotatePolicy"))
        policy = _policy_from_value(raw_policy)
        supported = (
            "auto-rotate" in capabilities
            if capabilities_present
            else raw_policy is not None
        )
        if supported and policy is None:
            policy = "never"
        if not supported:
            policy = None
        active.append(
            (
                PlasmaOutput(
                    name=name,
                    transform=transform,
                    auto_rotate=policy,
                    auto_rotate_supported=supported,
                ),
                item,
            )
        )
    if not active:
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-found")

    if forced_output:
        matches = [output for output, _item in active if output.name == forced_output]
        if len(matches) == 1:
            return matches[0]
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "requested-output-not-active")

    internal = [
        output for output, item in active if builtin_types(item) or _INTERNAL_NAME.match(output.name)
    ]
    if len(internal) == 1:
        return internal[0]
    if len(internal) > 1:
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-unique")
    if len(active) == 1:
        return active[0][0]
    raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-found")


def _doctor_builtin(item: dict) -> bool:
    output_type = item.get("type")
    if isinstance(output_type, int) and output_type == 7:  # KScreen::Output::Panel
        return True
    if isinstance(output_type, str) and output_type.strip().lower() in {
        "panel",
        "edp",
        "lvds",
        "dsi",
    }:
        return True
    return False


def _ctl_builtin(item: dict) -> bool:
    for key in ("builtin", "isBuiltin", "is-builtin"):
        if key in item:
            return bool(item[key])
    output_type = item.get("type")
    return isinstance(output_type, str) and output_type.strip().lower() in {"panel", "edp", "lvds", "dsi"}


def parse_doctor_json(text: str, forced_output: str | None = None) -> PlasmaOutput:
    """Parse ``kscreen-doctor -j`` (Plasma 6.0-6.8)."""

    return _select_output(text, forced_output, builtin_types=_doctor_builtin)


def parse_kscreenctl_json(text: str, forced_output: str | None = None) -> PlasmaOutput:
    """Parse ``kscreenctl list-outputs --json`` (Plasma 6.8+)."""

    return _select_output(text, forced_output, builtin_types=_ctl_builtin)


class PlasmaCommand:
    """Command adapter for one KScreen CLI generation."""

    name = ""
    binary = ""
    parse: Callable[[str, str | None], PlasmaOutput]

    def __init__(self, binary: str, runner: Callable[[list[str], float], object] | None = None):
        self.binary = binary
        self._runner = runner or run_command

    def query(self) -> str:
        raise NotImplementedError

    def set_rotation(self, output: str, transform: int) -> None:
        raise NotImplementedError

    def set_policy(self, output: str, policy: str) -> None:
        raise NotImplementedError

    def _run(self, args: list[str], code: str) -> str:
        try:
            result = self._runner(args, timeout=COMMAND_TIMEOUT_SECONDS)
        except FileNotFoundError as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, f"{self.name}-not-found") from error
        except subprocess.TimeoutExpired as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, f"{self.name}-timeout") from error
        except OSError as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, str(error)) from error
        if int(getattr(result, "returncode", 1)) != 0:
            detail = str(getattr(result, "stderr", "") or "").strip()
            raise DisplayError(code, detail[:200] or f"{self.name}-command-failed")
        return str(getattr(result, "stdout", "") or "")


class KScreenDoctorCommand(PlasmaCommand):
    name = "kscreen-doctor"
    parse = staticmethod(parse_doctor_json)

    def query(self) -> str:
        return self._run([self.binary, "-j"], ErrorCode.DISPLAY_UNAVAILABLE)

    def set_rotation(self, output: str, transform: int) -> None:
        rotation = _DOCTOR_ROTATIONS.get(transform)
        if rotation is None:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "unsupported-transform")
        self._run(
            [self.binary, f"output.{output}.rotation.{rotation}"],
            ErrorCode.DISPLAY_TRANSFORM,
        )

    def set_policy(self, output: str, policy: str) -> None:
        value = _DOCTOR_POLICIES.get(policy)
        if value is None:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "unsupported-rotation-policy")
        self._run(
            [self.binary, f"output.{output}.autoRotatePolicy.{value}"],
            ErrorCode.DISPLAY_LOCK,
        )


class KScreenCtlCommand(PlasmaCommand):
    name = "kscreenctl"
    parse = staticmethod(parse_kscreenctl_json)

    def query(self) -> str:
        return self._run([self.binary, "list-outputs", "--json"], ErrorCode.DISPLAY_UNAVAILABLE)

    def set_rotation(self, output: str, transform: int) -> None:
        rotation = _CTL_ROTATIONS.get(transform)
        if rotation is None:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "unsupported-transform")
        self._run(
            [self.binary, output, "set-rotation", rotation],
            ErrorCode.DISPLAY_TRANSFORM,
        )

    def set_policy(self, output: str, policy: str) -> None:
        value = _CTL_POLICIES.get(policy)
        if value is None:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "unsupported-rotation-policy")
        self._run(
            [self.binary, output, "set-auto-rotate", value],
            ErrorCode.DISPLAY_LOCK,
        )


class MissingPlasmaCommand(PlasmaCommand):
    """Fail at transaction time instead of crashing the session service."""

    name = "kscreen"
    parse = staticmethod(parse_doctor_json)

    def __init__(self, detail: str):
        self.binary = ""
        self._detail = detail
        self._runner = None

    def query(self) -> str:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, self._detail)

    def set_rotation(self, output: str, transform: int) -> None:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, self._detail)

    def set_policy(self, output: str, policy: str) -> None:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, self._detail)


def build_plasma_command(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[[list[str], float], object] | None = None,
) -> PlasmaCommand:
    kscreenctl = which("kscreenctl")
    if kscreenctl:
        return KScreenCtlCommand(kscreenctl, runner)
    doctor = which("kscreen-doctor")
    if doctor:
        return KScreenDoctorCommand(doctor, runner)
    return MissingPlasmaCommand("kscreenctl-or-kscreen-doctor-not-found")


class PlasmaDisplay(SoftwareDisplay):
    """Own the internal panel through KScreen and its auto-rotate policy."""

    backend_name = "plasma"

    def __init__(
        self,
        command: PlasmaCommand,
        journal: Journal | None = None,
        gettext: Callable[[str], str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        environ: Mapping[str, str] | None = None,
    ):
        self.command = command
        environment = os.environ if environ is None else environ
        self._forced_output = environment.get("PANASONIC_POSTURE_OUTPUT") or None
        super().__init__(journal, gettext, sleep=sleep)

    def _read_state(self) -> DisplayState:
        output = self.command.parse(self.command.query(), self._forced_output)
        return DisplayState(
            identity=(output.name,),
            transform=output.transform,
            lock=output.auto_rotate,
            lock_supported=output.auto_rotate_supported,
        )

    def _write_rotation(self, state: DisplayState, transform: int) -> None:
        for attempt in (0, 1):
            self.command.set_rotation(state.identity[0], transform)
            if self._wait_for(state, transform=transform):
                return
            if attempt == 1:
                raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch")

    def _write_lock(self, state: DisplayState, locked: bool) -> None:
        if not state.lock_supported:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "orientation-lock-unavailable")
        self._write_policy(state, self._lock_value(locked))

    def _lock_value(self, locked: bool) -> str:
        return "never" if locked else "in-tablet-mode"

    def _write_original_lock(self, state: DisplayState, policy: str) -> None:
        self._write_policy(state, policy)

    def _write_policy(self, state: DisplayState, policy: str) -> None:
        for attempt in (0, 1):
            self.command.set_policy(state.identity[0], policy)
            if self._wait_for(state, policy=policy):
                return
            if attempt == 1:
                raise DisplayError(ErrorCode.DISPLAY_LOCK, "policy-readback-mismatch")

    def _wait_for(
        self,
        state: DisplayState,
        *,
        transform: int | None = None,
        policy: str | None = None,
    ) -> bool:
        for delay in (0.0, READBACK_DELAY_SECONDS):
            if delay:
                self._sleep(delay)
            try:
                current = self._read_state()
            except DisplayError:
                return False
            if current.identity != state.identity:
                return False
            if transform is not None and current.transform != transform:
                continue
            if policy is not None and current.lock != policy:
                continue
            return True
        return False


__all__ = [
    "KScreenCtlCommand",
    "KScreenDoctorCommand",
    "MissingPlasmaCommand",
    "PlasmaCommand",
    "PlasmaDisplay",
    "PlasmaOutput",
    "build_plasma_command",
    "parse_doctor_json",
    "parse_kscreenctl_json",
]
