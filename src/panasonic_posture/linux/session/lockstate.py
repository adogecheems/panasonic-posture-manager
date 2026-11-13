"""Desktop-neutral screen-lock detection.

logind's ``LockedHint`` is the primary source because it is implemented by
GNOME, Plasma and other session managers.  Older or non-systemd sessions fall
back to the freedesktop and GNOME screen-saver interfaces.
"""

from __future__ import annotations

from gi.repository import GLib

LOGIN1_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"
LOGIN1_SESSION = "org.freedesktop.login1.Session"
PROPERTIES = "org.freedesktop.DBus.Properties"

SCREENSAVER_ENDPOINTS = (
    ("org.freedesktop.ScreenSaver", "/ScreenSaver", "org.freedesktop.ScreenSaver"),
    ("org.gnome.ScreenSaver", "/org/gnome/ScreenSaver", "org.gnome.ScreenSaver"),
)

TIMEOUT_MS = 700


def _unpack_boolean(reply) -> bool:
    value = reply.unpack()
    if isinstance(value, (tuple, list)):
        value = value[0]
    unpack = getattr(value, "unpack", None)
    if callable(unpack):
        value = unpack()
    return bool(value)


def login1_locked(bus) -> bool | None:
    """Return ``LockedHint`` for the caller's session, or None if unavailable."""

    if bus is None:
        return None
    try:
        reply = bus.call_sync(
            LOGIN1_NAME,
            LOGIN1_PATH,
            LOGIN1_MANAGER,
            "GetSession",
            GLib.Variant("(s)", ("auto",)),
            GLib.VariantType.new("(o)"),
            0,
            TIMEOUT_MS,
            None,
        )
        path = str(reply.unpack()[0])
        reply = bus.call_sync(
            LOGIN1_NAME,
            path,
            PROPERTIES,
            "Get",
            GLib.Variant("(ss)", (LOGIN1_SESSION, "LockedHint")),
            GLib.VariantType.new("(v)"),
            0,
            TIMEOUT_MS,
            None,
        )
        return _unpack_boolean(reply)
    except (GLib.Error, OSError, TypeError, ValueError, AttributeError, IndexError):
        return None


def screensaver_locked(bus) -> bool | None:
    """Try the freedesktop and GNOME screen-saver interfaces in order."""

    if bus is None:
        return None
    for destination, path, interface in SCREENSAVER_ENDPOINTS:
        try:
            reply = bus.call_sync(
                destination,
                path,
                interface,
                "GetActive",
                None,
                GLib.VariantType.new("(b)"),
                0,
                TIMEOUT_MS,
                None,
            )
            return _unpack_boolean(reply)
        except (GLib.Error, OSError, TypeError, ValueError, AttributeError, IndexError):
            continue
    return None


def query_locked(*, system_bus=None, session_bus=None) -> bool:
    """Return the current lock state; unknown or unavailable means unlocked."""

    if system_bus is not None:
        value = login1_locked(system_bus)
        if value is not None:
            return value
    if session_bus is not None:
        value = screensaver_locked(session_bus)
        if value is not None:
            return value
    return False


__all__ = [
    "LOGIN1_NAME",
    "LOGIN1_PATH",
    "SCREENSAVER_ENDPOINTS",
    "login1_locked",
    "query_locked",
    "screensaver_locked",
]
