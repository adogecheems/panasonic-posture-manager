"""GNOME temporary display changes with an ownership/recovery journal.

The session adapter is allowed to rotate the built-in panel and change the
GSettings rotation lock; every change is journaled before it is applied so a
crash can always be recovered on the next start.  The module is split into

* pure functions (:func:`internal_monitor`, :func:`rotation_plan`,
  :func:`desired_transform`) that translate Mutter's ``GetCurrentState`` into
  an ``ApplyMonitorsConfig`` request;
* :class:`Display`, the transaction that persists intent, writes the lock,
  rotates, verifies the result and rolls back on failure.

``DisplayError`` carries a stable :class:`protocol.ErrorCode`, never prose.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from gi.repository import Gio, GLib

from ..model import Target
from . import errors
from .errors import DisplayError, ErrorCode, describe_error
from .journal import Journal, default_path

log = logging.getLogger(__name__)

DEST = "org.gnome.Mutter.DisplayConfig"
OBJ = "/org/gnome/Mutter/DisplayConfig"

SETTINGS_SCHEMA = "org.gnome.settings-daemon.peripherals.touchscreen"
LOCK_KEY = "orientation-lock"

#: Software modes own the display transaction; hardware/unavailable give it back.
SOFTWARE_MODES = frozenset({"laptop", "tent", "stand", "tablet"})
RESTORE_MODES = frozenset({"hardware", "unavailable"})

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


def desired_transform(mode: str) -> int:
    """The Mutter transform this program owns for a software mode.

    Tablet mode starts in landscape before releasing the rotation lock; this
    avoids inheriting the 180 degree tent transform when the user switches
    modes (P2-17).
    """

    return 2 if mode == "tent" else 0


# ---------------------------------------------------------------------------
# Display transaction
# ---------------------------------------------------------------------------


class Display:
    """Own the built-in panel rotation/lock transaction for software modes.

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
        self._ = gettext or errors.get_translator()
        self._clock = clock
        self._sleep = sleep
        self.journal = journal if journal is not None else Journal(default_path())
        self.owned: dict | None = None
        self._recover()

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

    # -- public API -------------------------------------------------------

    def apply(self, target: Target | dict) -> None:
        """Apply *target*; raises :class:`DisplayError` on failure."""

        if isinstance(target, dict):
            target = Target.from_dict(target)
        mode = str(target.mode or "")
        if mode in RESTORE_MODES:
            self.restore()
            return
        if mode not in SOFTWARE_MODES:
            raise DisplayError(ErrorCode.DISPLAY_UNKNOWN_MODE, mode or "empty")
        if mode == "laptop":
            if self.owned:
                self.restore(normal=True)
            else:
                state = self.state()
                spec, _current = internal_monitor(state)
                self.rotate(0, spec)
            return

        state = self.state()
        spec, current = internal_monitor(state)
        transform = desired_transform(mode)
        rotation_plan(state, spec, transform)  # validate before persistent changes
        if not self.owned:
            self.owned = {
                "version": 1,
                "spec": [str(part) for part in spec],
                "original_transform": int(current),
                "original_lock": self._read_lock(),
                "written_lock": None,
                "written_transform": None,
            }
            self._persist()  # durable ownership before touching anything
        try:
            if mode == "tablet":
                # P2-17: rotate to landscape while the rotation lock still
                # holds, then release the lock so GNOME may auto-rotate.
                self.owned["written_lock"] = True
                self.owned["written_transform"] = 0
                self._persist()
                if not self._read_lock():
                    self._write_lock(True)
                self.rotate(0, spec)
                self.owned["written_lock"] = False
                self._persist()
                self._write_lock(False)
            else:
                self.owned["written_lock"] = True
                self.owned["written_transform"] = transform
                self._persist()
                self._write_lock(True)
                self.rotate(transform, spec)
        except (DisplayError, GLib.Error, OSError, ValueError):
            self._rollback()
            raise

    def restore(self, normal: bool = False) -> None:
        """Undo the journaled changes; keep the journal on failure (P2-17)."""

        owned = self.owned
        if not owned:
            return
        state = self.state()  # display server failures propagate, journal kept
        spec, current = internal_monitor(state)
        journal_spec = tuple(str(part) for part in owned.get("spec") or ())
        written_transform = owned.get("written_transform")
        failure: BaseException | None = None

        if written_transform is not None and spec == journal_spec:
            if current == written_transform:
                original_transform = int(owned.get("original_transform") or 0)
                try:
                    self.rotate(0 if normal else original_transform, spec)
                except (DisplayError, GLib.Error, OSError, ValueError) as error:
                    failure = error
            else:
                # Another actor already changed the rotation; do not fight it.
                log.debug("display rotation already %s; not restoring", current)
        elif written_transform is not None:
            log.warning("internal monitor changed; leaving rotation untouched")

        written_lock = owned.get("written_lock")
        if written_lock is not None:
            try:
                self._restore_lock(bool(owned.get("original_lock")))
            except DisplayError as error:
                failure = failure or error

        if failure is not None:
            raise failure
        self.journal.clear()
        self.owned = None

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

    def _write_lock(self, value: bool) -> None:
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

    def _restore_lock(self, original: bool) -> None:
        if self._read_lock() == original:
            return
        self._write_lock(original)

    # -- internals --------------------------------------------------------

    def _persist(self) -> None:
        self.journal.save(self.owned)  # type: ignore[arg-type]

    def _rollback(self) -> None:
        try:
            self.restore()
        except (DisplayError, GLib.Error, OSError, ValueError) as error:
            log.warning("display rollback failed; journal kept: %s", describe_error(error))

    def _recover(self) -> None:
        document = self.journal.load()
        if not document:
            return
        self.owned = document
        try:
            self.restore()
        except (DisplayError, GLib.Error, OSError, ValueError) as error:
            # Mutter may not be ready during early session startup; keep the
            # journal so the next transaction (or the next start) retries.
            log.warning("display recovery pending: %s", describe_error(error))


__all__ = [
    "APPLY_TIMEOUT_MS",
    "DEST",
    "Display",
    "OBJ",
    "READBACK_DELAY_SECONDS",
    "SETTINGS_SCHEMA",
    "SOFTWARE_MODES",
    "desired_transform",
    "internal_monitor",
    "rotation_plan",
]
