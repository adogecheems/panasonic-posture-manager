"""Observe greeter and monitor power state without changing desktop settings."""
from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Callable

from gi.repository import Gio, GLib

from .lockstate import LOGIN1_NAME, LOGIN1_SESSION, PROPERTIES


class ScreenContext:
    def __init__(self, greeter: bool, drm: Path = Path("/sys/class/drm"),
                 monitor_power: Callable[[], int | None] | None = None):
        self.greeter = greeter
        self.drm = drm
        self.monitor_power = monitor_power

    def _screens_off(self) -> bool:
        if self.monitor_power is not None:
            mode = self.monitor_power()
            if mode in (0, 1, 2, 3):
                return mode != 0
        try:
            states = [status.with_name("dpms").read_text().strip()
                      for status in self.drm.glob("card*-*/status")
                      if status.read_text().strip() == "connected"]
        except OSError:
            return False
        return bool(states) and all(s in ("Off", "Standby", "Suspend") for s in states)

    def __call__(self) -> bool:
        if self.greeter:
            return True
        return self._screens_off()


def desktop_context(system_bus, session_bus) -> ScreenContext:
    try:
        reply = system_bus.call_sync(
            LOGIN1_NAME, "/org/freedesktop/login1/session/auto", PROPERTIES, "Get",
            GLib.Variant("(ss)", (LOGIN1_SESSION, "Class")), GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NO_AUTO_START, 250, None,
        )
        greeter = reply.unpack()[0] == "greeter"
    except GLib.Error:
        greeter = os.environ.get("XDG_SESSION_CLASS") == "greeter"

    context = ScreenContext(greeter)
    if not greeter and "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":"):
        interface = "org.gnome.Mutter.DisplayConfig"

        def proxy_ready(_source, result):
            try:
                proxy = Gio.DBusProxy.new_finish(result)
            except GLib.Error:
                return  # DRM also covers compositor startup and restarts.

            def power_mode():
                variant = proxy.get_cached_property("PowerSaveMode")
                return variant.unpack() if variant is not None else None

            context.monitor_power = power_mode

        Gio.DBusProxy.new(
            session_bus, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None, interface,
            "/org/gnome/Mutter/DisplayConfig", interface, None, proxy_ready,
        )
    return context
