"""X11 display backend using RandR (``xrandr``).

X11 has no portable automatic-rotation lock, so this backend only owns the
explicit panel transform.  On GNOME X11 the Mutter backend is selected
instead, because it also owns the orientation lock.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .errors import DisplayError, ErrorCode
from .journal import Journal
from .transaction import DisplayState, SoftwareDisplay
from .x11_touch import X11TouchMapping

COMMAND_TIMEOUT_SECONDS = 2.5
READBACK_DELAY_SECONDS = 0.15

ROTATION_TO_TRANSFORM = {"normal": 0, "left": 1, "inverted": 2, "right": 3}
TRANSFORM_TO_ROTATION = {value: key for key, value in ROTATION_TO_TRANSFORM.items()}

_OUTPUT_LINE = re.compile(
    r"^(?P<name>\S+)\s+(?P<status>connected|disconnected|unknown connection)\b(?P<rest>.*)$"
)
_ACTIVE_GEOMETRY = re.compile(r"\b\d+x\d+\+-?\d+\+-?\d+\b")
_ROTATION_BLOCK = re.compile(r"\(([^)]*)\)")
_INTERNAL_NAME = re.compile(r"^(?:edp|ldp|lvds|dsi)-?\d*$", re.IGNORECASE)


@dataclass(frozen=True)
class X11Output:
    name: str
    transform: int


def _pick_output(outputs: list[X11Output], forced_output: str | None) -> X11Output:
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
        # Single-head systems without an eDP/LVDS/DSI name.  This keeps X11
        # support usable on unusual connectors without guessing between heads.
        return outputs[0]
    raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-found")


def parse_xrandr_query(text: str, forced_output: str | None = None) -> X11Output:
    """Return the active internal RandR output and its current transform."""

    outputs: list[X11Output] = []
    for line in str(text or "").splitlines():
        match = _OUTPUT_LINE.match(line)
        if match is None or match.group("status") != "connected":
            continue
        rest = match.group("rest")
        if _ACTIVE_GEOMETRY.search(rest) is None:
            # A connected but disabled output has no active mode/geometry.
            continue
        rotation = _ROTATION_BLOCK.search(rest)
        tokens = rotation.group(1).split() if rotation else []
        current = tokens[0].lower() if tokens else ""
        transform = ROTATION_TO_TRANSFORM.get(current)
        if transform is None:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "current-rotation-unavailable")
        outputs.append(X11Output(match.group("name"), transform))

    if not outputs:
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-found")
    return _pick_output(outputs, forced_output)


def run_command(args: list[str], timeout: float):
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class X11Display(SoftwareDisplay):
    """Own the internal panel transform through ``xrandr``."""

    backend_name = "x11"

    def __init__(
        self,
        runner: Callable[[list[str], float], object] | None = None,
        journal: Journal | None = None,
        gettext: Callable[[str], str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        environ: Mapping[str, str] | None = None,
        touch: X11TouchMapping | None = None,
    ):
        self._runner = runner or run_command
        environment = os.environ if environ is None else environ
        self._forced_output = environment.get("PANASONIC_POSTURE_OUTPUT") or None
        self._touch = touch
        super().__init__(journal, gettext, sleep=sleep)

    def _read_state(self) -> DisplayState:
        output = parse_xrandr_query(self._query(), self._forced_output)
        return DisplayState(
            identity=(output.name,),
            transform=output.transform,
            lock=True,
            lock_supported=False,
            auxiliary=self._touch.capture() if self._touch is not None else None,
        )

    def _query(self) -> str:
        return self._run(["xrandr", "--query"], ErrorCode.DISPLAY_UNAVAILABLE)

    def _write_rotation(self, state: DisplayState, transform: int) -> None:
        rotation = TRANSFORM_TO_ROTATION.get(transform)
        if rotation is None:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "unsupported-transform")
        output = state.identity[0]
        for attempt in (0, 1):
            self._run(
                ["xrandr", "--output", output, "--rotate", rotation],
                ErrorCode.DISPLAY_TRANSFORM,
            )
            if self._wait_for_transform(output, transform):
                return
            if attempt == 1:
                raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch")

    def _wait_for_transform(self, output: str, transform: int) -> bool:
        for delay in (0.0, READBACK_DELAY_SECONDS):
            if delay:
                self._sleep(delay)
            try:
                current = parse_xrandr_query(self._query(), output).transform
            except DisplayError:
                return False
            if current == transform:
                return True
        return False

    def _write_lock(self, state: DisplayState, locked: bool) -> None:
        raise DisplayError(ErrorCode.DISPLAY_LOCK, "x11-orientation-lock-unsupported")

    def _write_original_lock(self, state: DisplayState, policy: str) -> None:
        raise DisplayError(ErrorCode.DISPLAY_LOCK, "x11-orientation-lock-unsupported")

    def _write_auxiliary(self, state: DisplayState) -> None:
        if self._touch is None or state.auxiliary is None:
            return
        self._touch.map_to_output(state.auxiliary, state.identity[0])

    def _restore_auxiliary(self, state: DisplayState, original: dict) -> None:
        del state
        if self._touch is None:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "xinput-not-configured")
        self._touch.restore(original)

    def _run(self, args: list[str], code: str) -> str:
        try:
            result = self._runner(args, timeout=COMMAND_TIMEOUT_SECONDS)
        except FileNotFoundError as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "xrandr-not-found") from error
        except subprocess.TimeoutExpired as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "xrandr-timeout") from error
        except OSError as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, str(error)) from error
        if int(getattr(result, "returncode", 1)) != 0:
            detail = str(getattr(result, "stderr", "") or "").strip()
            raise DisplayError(code, detail[:200] or "xrandr-command-failed")
        return str(getattr(result, "stdout", "") or "")


__all__ = [
    "COMMAND_TIMEOUT_SECONDS",
    "READBACK_DELAY_SECONDS",
    "ROTATION_TO_TRANSFORM",
    "TRANSFORM_TO_ROTATION",
    "X11Display",
    "X11Output",
    "parse_xrandr_query",
    "run_command",
]
