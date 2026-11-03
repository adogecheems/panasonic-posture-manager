"""Desktop/session detection and display backend selection.

The privileged daemon and relay are desktop-neutral.  Only the unprivileged
session adapter needs desktop integration, so this module keeps the selection
logic in one place and lets unsupported sessions start the adapter with a
stable error instead of crashing in a systemd restart loop.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol

from .display import Display
from .errors import DisplayError, ErrorCode
from .journal import Journal
from .plasma import PlasmaDisplay, build_plasma_command

BACKEND_OVERRIDE_ENV = "PANASONIC_POSTURE_DISPLAY_BACKEND"


class DisplayBackend(Protocol):
    """Minimum interface the session runtime needs from a display backend."""

    def apply(self, target) -> None: ...

    def restore(self, normal: bool = False) -> None: ...


@dataclass(frozen=True)
class DisplayContext:
    """Inputs shared by backend factories."""

    session_bus: object
    journal: Journal
    gettext: Callable[[str], str]
    environment: Mapping[str, str]
    which: Callable[[str], str | None]
    runner: Callable[[list[str], float], object] | None


BackendFactory = Callable[[DisplayContext], DisplayBackend]


def _session_type(environ: Mapping[str, str]) -> str:
    value = str(environ.get("XDG_SESSION_TYPE", "")).strip().lower()
    if value:
        return value
    if environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return ""


def detect_backend(environ: Mapping[str, str] | None = None) -> str:
    """Return ``gnome``, ``plasma`` or ``unsupported``."""

    environment = os.environ if environ is None else environ
    session_type = _session_type(environment)
    if session_type != "wayland":
        return "unsupported"

    override = str(environment.get(BACKEND_OVERRIDE_ENV, "")).strip().lower()
    if override and override != "auto":
        return override if override in SUPPORTED_BACKENDS else "unsupported"

    desktops = {
        part.strip().upper()
        for part in str(environment.get("XDG_CURRENT_DESKTOP", "")).split(":")
        if part.strip()
    }
    if "GNOME" in desktops:
        return "gnome"

    if "KDE" in desktops or "PLASMA" in desktops:
        version = str(environment.get("KDE_SESSION_VERSION", "")).strip()
        if version and version != "6":
            return "unsupported"
        return "plasma"

    return "unsupported"


class UnsupportedDisplay:
    """A display object that reports a stable error instead of crashing."""

    def __init__(self, reason: str):
        self.reason = reason

    def apply(self, _target) -> None:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, f"unsupported-desktop:{self.reason}")

    def restore(self, normal: bool = False) -> None:
        del normal  # nothing was changed by this backend


def _build_gnome(context: DisplayContext) -> DisplayBackend:
    return Display(context.session_bus, None, context.journal, context.gettext)


def _build_plasma(context: DisplayContext) -> DisplayBackend:
    return PlasmaDisplay(
        build_plasma_command(which=context.which, runner=context.runner),
        journal=context.journal,
        gettext=context.gettext,
        environ=context.environment,
    )


DISPLAY_BACKENDS: Mapping[str, BackendFactory] = {
    "gnome": _build_gnome,
    "plasma": _build_plasma,
}
SUPPORTED_BACKENDS = frozenset(DISPLAY_BACKENDS)


def create_display(
    session_bus,
    journal_path,
    gettext,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[[list[str], float], object] | None = None,
):
    """Build the display backend for the current desktop session."""

    environment = os.environ if environ is None else environ
    backend = detect_backend(environment)
    journal = Journal(journal_path)
    factory = DISPLAY_BACKENDS.get(backend)
    if factory is not None:
        return factory(
            DisplayContext(
                session_bus=session_bus,
                journal=journal,
                gettext=gettext,
                environment=environment,
                which=which,
                runner=runner,
            )
        )
    reason = (
        environment.get("XDG_CURRENT_DESKTOP")
        or environment.get("XDG_SESSION_TYPE")
        or "unknown"
    )
    return UnsupportedDisplay(str(reason))


__all__ = [
    "BACKEND_OVERRIDE_ENV",
    "DISPLAY_BACKENDS",
    "DisplayBackend",
    "DisplayContext",
    "SUPPORTED_BACKENDS",
    "UnsupportedDisplay",
    "create_display",
    "detect_backend",
]
