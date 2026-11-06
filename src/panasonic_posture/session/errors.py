"""Stable error codes, localized user-facing text and session error types.

The privileged daemon reports a machine readable :class:`protocol.ErrorCode`
plus a free-form ``detail`` that is meant for logs only.  This module is the
shared code -> message mapping used by both the session adapter and Python UI.  ``tests/check_i18n.py`` extracts every
translatable literal from this file and verifies the gettext catalog covers
them.

The translation catalog is resolved at import time.  Installed systems have
``/usr/share/locale/<lang>/LC_MESSAGES/panasonic-posture-manager.mo``; a
source checkout falls back to ``<repo>/locale`` (the directory ``make``
builds).  No language is hard coded: any catalog present in either root is
used according to the user's locale.
"""

from __future__ import annotations

import gettext as _gettext
import os
from pathlib import Path
from typing import Callable

from ..linux_contract import LOCALE_DIR, LOCALE_DOMAIN
from ..contracts import ErrorCode
from .failures import (
    DisplayError as DisplayError, SessionError as SessionError,
    ServiceError as ServiceError, describe_error as describe_error,
)

Translator = Callable[[str], str]


def _has_catalog(root: Path) -> bool:
    """Return True when *root* is a locale directory containing our catalog."""
    try:
        return any(root.glob(f"*/LC_MESSAGES/{LOCALE_DOMAIN}.mo"))
    except OSError:  # pragma: no cover - unreadable /usr/share/locale
        return False


def locate_locale_root() -> str | None:
    """Locate the locale root for gettext, preferring the installed catalog.

    ``PANASONIC_POSTURE_LOCALE_DIR`` is an explicit escape hatch used by
    development and integration tests.
    """

    override = os.environ.get("PANASONIC_POSTURE_LOCALE_DIR")
    if override:
        return override
    installed = Path(LOCALE_DIR)
    if _has_catalog(installed):
        return str(installed)
    source = Path(__file__).resolve().parents[3] / "locale"
    if _has_catalog(source):
        return str(source)
    return None


_TRANSLATION = _gettext.translation(
    LOCALE_DOMAIN, localedir=locate_locale_root(), fallback=True
)
_ = _TRANSLATION.gettext


def get_translator() -> Translator:
    """Return the process-wide gettext function used by the session adapter."""

    return _


# msgids are user visible: keep them short, neutral and free of details.
ERROR_MESSAGES: dict[str, str] = {
    ErrorCode.INVALID_MODE: _("This mode is not available right now."),
    ErrorCode.ACCESS_DENIED: _("Please switch back to the local desktop and try again."),
    ErrorCode.UNSUPPORTED_MODEL: _("This model does not support automatic mode changes."),
    ErrorCode.HARDWARE_PRIORITY: _("The tablet switch currently controls the mode."),
    ErrorCode.EXPIRED_CONFIRMATION: _(
        "The posture prompt has expired. Wait for it to appear again."
    ),
    ErrorCode.CONFIG_WRITE: _("The setting could not be saved. Please try again."),
    ErrorCode.RELAY_START: _(
        "The internal keyboard and touchpad could not be controlled. Please try again."
    ),
    ErrorCode.RELAY_EXITED: _("Internal input control stopped. Please try again."),
    ErrorCode.RELAY_HEARTBEAT: _(
        "Internal input control stopped responding. Please try again."
    ),
    ErrorCode.RELAY_INPUT: _(
        "The internal keyboard and touchpad could not be disabled. Please try again."
    ),
    ErrorCode.NATIVE_RECOVERY: _("The tablet switch state could not be restored. Restart the service if input behaves unexpectedly."),
    ErrorCode.SENSOR_TIMEOUT: _("No posture data is available right now."),
    ErrorCode.CALIBRATION_NOT_READY: _(
        "Place the device in laptop form on a flat desk and hold it still for about two seconds, then try again."
    ),
    ErrorCode.DISPLAY_MIRRORED: _(
        "This screen arrangement cannot be rotated. Please adjust the display settings."
    ),
    ErrorCode.DISPLAY_LAYOUT: _(
        "The external display arrangement does not allow this mode."
    ),
    ErrorCode.DISPLAY_TRANSFORM: _(
        "The screen rotation could not be applied. Please try again."
    ),
    ErrorCode.DISPLAY_LOCK: _(
        "The rotation lock could not be changed. Please try again."
    ),
    ErrorCode.DISPLAY_UNAVAILABLE: _("Screen rotation is not available right now."),
    ErrorCode.DISPLAY_UNKNOWN_MODE: _("This display mode is not supported."),
    ErrorCode.DISPLAY_IDENTITY: _(
        "The internal screen changed while applying the mode. Please try again."
    ),
    ErrorCode.INTERNAL: _("Something went wrong. Please try again."),
}


def error_text(code: str | None) -> str:
    """Return the localized message for a protocol error code."""

    key = code if code in ERROR_MESSAGES else ErrorCode.INTERNAL
    return ERROR_MESSAGES[key]


__all__ = [
    "DisplayError",
    "ERROR_MESSAGES",
    "ServiceError",
    "SessionError",
    "Translator",
    "describe_error",
    "error_text",
    "get_translator",
    "locate_locale_root",
]
