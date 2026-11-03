"""GNOME display backend for the shared ownership/recovery transaction.

The session adapter is allowed to rotate the built-in panel and change the
GSettings rotation lock; every change is journaled before it is applied so a
crash can always be recovered on the next start.  The module is split into

* pure functions (:func:`internal_monitor`, :func:`rotation_plan`) that
  translate Mutter's ``GetCurrentState`` into an ``ApplyMonitorsConfig``
  request;
* :class:`Display`, which implements only the Mutter/GSettings primitives used
  by :class:`transaction.SoftwareDisplay` for journaling, recovery and retry.

``DisplayError`` carries a stable :class:`protocol.ErrorCode`, never prose.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from gi.repository import Gio, GLib

from . import errors
from .errors import DisplayError, ErrorCode, describe_error
from .journal import Journal, default_path
from .transaction import (
    RESTORE_MODES,
    SOFTWARE_MODES,
    DisplayState,
    SoftwareDisplay,
    desired_transform,
)

log = logging.getLogger(__name__)

DEST = "org.gnome.Mutter.DisplayConfig"
OBJ = "/org/gnome/Mutter/DisplayConfig"

SETTINGS_SCHEMA = "org.gnome.settings-daemon.peripherals.touchscreen"
LOCK_KEY = "orientation-lock"

STATE_TIMEOUT_MS = 2000
APPLY_TIMEOUT_MS = 2500
READBACK_DELAY_SECONDS = 0.15


# ---------------------------------------------------------------------------
# Pure planning functions (unit tested without a display server)
# ---------------------------------------------------------------------------


def internal_monitor(state) -> tuple[tuple, int]:
    """Return ``(monitor_spec, current_transform)`` of the built-in panel."""

    try:
        _serial, monitors, logical, _properties = state
    except (TypeError, ValueError):
        raise DisplayError(ErrorCode.INTERNAL, "invalid display state") from None
    matches = [
        spec
        for spec, _modes, props in monitors
        if isinstance(props, dict) and props.get("is-builtin")
    ]
    if len(matches) != 1:
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-not-unique")
    spec = tuple(matches[0])
    active = [
        mode for mode in logical if spec in [tuple(candidate) for candidate in mode[5]]
    ]
    if len(active) != 1 or len(active[0][5]) != 1:
        raise DisplayError(ErrorCode.DISPLAY_MIRRORED, "internal-monitor-mirrored-or-disabled")
    return spec, active[0][3]


def rotation_plan(state, spec, transform):
    """Build the ``ApplyMonitorsConfig`` arguments rotating only *spec*."""

    serial, monitors, logical, properties = state
    current = {tuple(monitor): (modes, props) for monitor, modes, props in monitors}
    target_spec = tuple(spec)
    rows = []
    found = False
    for x, y, scale, old_transform, primary, specs, _ in logical:
        contains = target_spec in [tuple(candidate) for candidate in specs]
        if contains:
            if len(specs) != 1:
                raise DisplayError(ErrorCode.DISPLAY_MIRRORED, "mirrored-internal-monitor")
            if old_transform % 2 != transform % 2 and len(logical) > 1:
                raise DisplayError(ErrorCode.DISPLAY_LAYOUT, "external-layout-needs-manual-adjustment")
            found = True
        outputs = []
        for monitor in specs:
            modes, props = current[tuple(monitor)]
            active = [mode[0] for mode in modes if mode[-1].get("is-current")]
            if len(active) != 1:
                raise DisplayError(
                    ErrorCode.DISPLAY_UNAVAILABLE, "current-monitor-mode-unavailable"
                )
            output_props = {}
            for source, destination, kind in [
                ("is-underscanning", "underscanning", "b"),
                ("color-mode", "color-mode", "u"),
                ("rgb-range", "rgb-range", "u"),
            ]:
                if source in props:
                    output_props[destination] = GLib.Variant(kind, props[source])
            outputs.append((monitor[0], active[0], output_props))
        rows.append(
            (x, y, scale, transform if contains else old_transform, primary, outputs)
        )
    if not found:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, "internal-monitor-disconnected")
    properties_out = (
        {"layout-mode": GLib.Variant("u", properties["layout-mode"])}
        if "layout-mode" in properties
        else {}
    )
    return serial, rows, properties_out


# ---------------------------------------------------------------------------
# Display transaction
# ---------------------------------------------------------------------------


class Display(SoftwareDisplay):
    """Own the built-in panel rotation/lock through Mutter's DisplayConfig.

    All collaborators are injectable:

    * ``session_bus``: object with ``call_sync`` (a ``Gio.DBusConnection``);
    * ``settings``: object with ``get_boolean(key)``/``set_boolean(key, value)``
      and ``sync()`` (a ``Gio.Settings``);
    * ``journal``: :class:`journal.Journal` (or a compatible fake);
    * ``gettext``: callable used for user-facing text (the display layer only
      logs, but the dependency is kept for consistency and translations).
    """

    def __init__(
        self,
        session_bus=None,
        settings=None,
        journal: Journal | None = None,
        gettext: Callable[[str], str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._bus = session_bus
        self._settings = settings
        self._clock = clock
        super().__init__(
            journal if journal is not None else Journal(default_path()),
            gettext or errors.get_translator(),
            sleep=sleep,
        )

    backend_name = "gnome"

    # -- injected collaborators ------------------------------------------

    @property
    def bus(self):
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._bus

    @property
    def settings(self):
        if self._settings is None:
            self._settings = Gio.Settings.new(SETTINGS_SCHEMA)
        return self._settings

    # -- backend contract -------------------------------------------------

    def _read_state(self) -> DisplayState:
        state = self.state()
        spec, current = internal_monitor(state)
        return DisplayState(
            identity=tuple(str(part) for part in spec),
            transform=int(current),
            lock=self._read_lock(),
            lock_supported=True,
        )

    def _validate(self, state: DisplayState, mode: str, transform: int) -> None:
        del mode
        rotation_plan(self.state(), state.identity, transform)

    def _write_rotation(self, state: DisplayState, transform: int) -> None:
        self.rotate(transform, state.identity)

    def _write_lock(self, state: DisplayState, locked: bool) -> None:
        del state
        self._set_lock(locked)

    def _write_original_lock(self, state: DisplayState, policy: str) -> None:
        del state, policy
        raise DisplayError(ErrorCode.DISPLAY_LOCK, "unsupported-original-lock-policy")

    # -- Mutter calls -----------------------------------------------------

    def state(self):
        try:
            reply = self.bus.call_sync(
                DEST,
                OBJ,
                DEST,
                "GetCurrentState",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                STATE_TIMEOUT_MS,
                None,
            )
        except GLib.Error as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, describe_error(error)) from error
        value = reply.unpack()
        if not isinstance(value, tuple) or len(value) != 4:
            raise DisplayError(ErrorCode.INTERNAL, "invalid DisplayConfig state")
        return value

    def rotate(self, transform: int, spec) -> None:
        """Rotate the built-in panel, retrying once and verifying the result."""

        target_spec = tuple(spec)
        for attempt in (0, 1):
            state = self.state()
            current_spec, current = internal_monitor(state)
            if current_spec != target_spec:
                raise DisplayError(ErrorCode.DISPLAY_IDENTITY, "internal-monitor-identity-changed")
            if current == transform:
                return
            serial, rows, properties = rotation_plan(state, current_spec, transform)
            try:
                self.bus.call_sync(
                    DEST,
                    OBJ,
                    DEST,
                    "ApplyMonitorsConfig",
                    GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})", (serial, 1, rows, properties)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    APPLY_TIMEOUT_MS,
                    None,
                )
            except GLib.Error as error:
                if attempt == 0:
                    continue
                raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, describe_error(error)) from error
            if self._wait_transform(transform):
                return
            if attempt == 1:
                raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback-mismatch")

    def _wait_transform(self, transform: int) -> bool:
        for delay in (0.0, READBACK_DELAY_SECONDS):
            if delay:
                self._sleep(delay)
            try:
                if internal_monitor(self.state())[1] == transform:
                    return True
            except (DisplayError, GLib.Error, OSError):
                return False
        return False

    # -- GSettings rotation lock -----------------------------------------

    def _read_lock(self) -> bool:
        try:
            return bool(self.settings.get_boolean(LOCK_KEY))
        except (GLib.Error, AttributeError, TypeError) as error:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, describe_error(error)) from error

    def _set_lock(self, value: bool) -> None:
        """Write the lock and verify both the return value and a read-back."""

        value = bool(value)
        try:
            written = self.settings.set_boolean(LOCK_KEY, value)
        except (GLib.Error, AttributeError, TypeError) as error:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, describe_error(error)) from error
        sync = getattr(self.settings, "sync", None)
        if callable(sync):
            sync()
        if written is False:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "set_boolean-rejected")
        try:
            readback = bool(self.settings.get_boolean(LOCK_KEY))
        except (GLib.Error, AttributeError, TypeError) as error:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, describe_error(error)) from error
        if readback != value:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "orientation-lock-readback-mismatch")

__all__ = [
    "APPLY_TIMEOUT_MS",
    "DEST",
    "Display",
    "OBJ",
    "READBACK_DELAY_SECONDS",
    "RESTORE_MODES",
    "SETTINGS_SCHEMA",
    "SOFTWARE_MODES",
    "desired_transform",
    "internal_monitor",
    "rotation_plan",
]
