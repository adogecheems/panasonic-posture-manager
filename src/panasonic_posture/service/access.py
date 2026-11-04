"""D-Bus authorization helpers for the privileged service.

The daemon accepts a method call only when it comes from the active local
graphical session of ``seat0``; state-changing calls additionally require the
polkit action configured for this project.

``SessionUpdate`` is authenticated by :meth:`AccessControl.verify_session_adapter`
through four independent checks:

(a) resolve the caller uid with ``GetConnectionUnixUser``;
(b) require the sender to be the current owner of the per-uid adapter name
    ``protocol.session_name(uid)`` (``GetNameOwner``);
(c) require the uid to have an active local graphical session on ``seat0``
    (logind, cached for at most one second so a 2 Hz heartbeat does not call
    logind on every tick);
(d) require the sender process to belong to the systemd user unit
    ``panasonic-posture-manager-session.service`` (``GetConnectionUnixProcessID``
    plus ``/proc/<pid>/cgroup``); an unreadable cgroup is rejected.

Everything here is pure D-Bus plumbing and can be unit tested with a fake
connection object; no real bus, logind or polkit service is required.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from gi.repository import GLib

from ..protocol import (
    AUTH_CACHE_SECONDS,
    POLKIT_ACTION,
    SESSION_CHECK_SECONDS,
    session_name as adapter_name,
)

LOG = logging.getLogger(__name__)

LOGIN1_NAME = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_IFACE = "org.freedesktop.login1.Manager"
LOGIN1_SESSION_IFACE = "org.freedesktop.login1.Session"

POLKIT_NAME = "org.freedesktop.PolicyKit1"
POLKIT_PATH = "/org/freedesktop/PolicyKit1/Authority"
POLKIT_IFACE = "org.freedesktop.PolicyKit1.Authority"

DBUS_NAME = "org.freedesktop.DBus"
DBUS_PATH = "/org/freedesktop/DBus"
DBUS_IFACE = "org.freedesktop.DBus"

SEAT = "seat0"
SESSION_TYPES = ("wayland",)

#: systemd *user* unit that runs the unprivileged session adapter.
SESSION_UNIT = "panasonic-posture-manager-session.service"

# Tightened timeouts (review Q-03): every one of these runs synchronously on the
# daemon main loop, so a slow logind/polkit must not stall the 100 ms tick for
# several seconds.  The previous 1500/3000 ms values could block ~7.5 s for a
# cold authorization.
LOGIND_TIMEOUT_MS = 800
POLKIT_TIMEOUT_MS = 1500
CACHE_LIMIT = 64
PROC_ROOT = Path("/proc")


class AccessDenied(PermissionError):
    """The caller is not the active local graphical user."""


@dataclass(frozen=True)
class SessionInfo:
    """The active graphical login session found by logind."""

    uid: int
    session_id: str
    path: str
    seat: str = SEAT
    type: str = ""


class AccessControl:
    """Authorize D-Bus senders against logind/polkit.

    Parameters
    ----------
    bus:
        A ``Gio.DBusConnection``-like object; ``None`` until
        :meth:`attach` is called by the daemon once its bus connection exists.
    monotonic:
        Injectable clock used by the authorization cache.
    """

    def __init__(
        self,
        bus: Any = None,
        *,
        cache_seconds: float = AUTH_CACHE_SECONDS,
        session_cache_seconds: float = SESSION_CHECK_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger = LOG,
        polkit_action: str = POLKIT_ACTION,
        proc_root: str | Path = PROC_ROOT,
        session_unit: str = SESSION_UNIT,
    ) -> None:
        self._bus = bus
        self._cache_seconds = float(cache_seconds)
        self._session_cache_seconds = float(session_cache_seconds)
        self._monotonic = monotonic
        self._log = logger
        self._polkit_action = polkit_action
        self._proc_root = Path(proc_root)
        self._session_unit = session_unit
        # sender -> (monotonic timestamp, uid); successful authorization only.
        self._cache: dict[str, tuple[float, int]] = {}
        # sender -> (uid, pid); immutable for the lifetime of a bus connection.
        self._identity_cache: dict[str, tuple[int, int]] = {}
        # uid -> (monotonic timestamp, SessionInfo); positive results only and
        # deliberately short lived: only the *current* active session may keep
        # refreshing the adapter lease.
        self._session_cache: dict[int, tuple[float, SessionInfo]] = {}

    # -- plumbing --------------------------------------------------------

    def attach(self, bus: Any) -> None:
        """Bind the real bus connection and drop every cached authorization."""
        self._bus = bus
        self._cache.clear()
        self._identity_cache.clear()
        self._session_cache.clear()

    def _call(
        self,
        destination: str,
        path: str,
        interface: str,
        method: str,
        parameters: GLib.Variant | None,
        reply_signature: str,
        timeout_ms: int,
    ) -> tuple:
        if self._bus is None:
            raise AccessDenied("D-Bus connection is not available")
        try:
            reply = self._bus.call_sync(
                destination,
                path,
                interface,
                method,
                parameters,
                GLib.VariantType.new(reply_signature),
                0,  # Gio.DBusCallFlags.NONE
                timeout_ms,
                None,
            )
        except GLib.Error as error:
            raise AccessDenied(f"{method} failed: {error.message}") from None
        try:
            return reply.unpack()
        except (TypeError, ValueError) as error:  # pragma: no cover - defensive
            raise AccessDenied(f"{method} returned an invalid reply: {error}") from None

    # -- identity --------------------------------------------------------

    def caller_uid(self, sender: str) -> int:
        """Return the Unix uid owning the bus name ``sender``."""
        if not sender:
            raise AccessDenied("missing sender")
        try:
            values = self._call(
                DBUS_NAME,
                DBUS_PATH,
                DBUS_IFACE,
                "GetConnectionUnixUser",
                GLib.Variant("(s)", (sender,)),
                "(u)",
                LOGIND_TIMEOUT_MS,
            )
        except AccessDenied as error:
            self._log.warning("cannot resolve uid of %s: %s", sender, error)
            raise
        try:
            return int(values[0])
        except (IndexError, TypeError, ValueError):  # pragma: no cover - defensive
            raise AccessDenied(f"GetConnectionUnixUser returned {values!r}") from None

    def session_name_owner(self, name: str) -> str | None:
        """Return the current owner of the well-known bus name ``name``."""
        if self._bus is None:
            raise AccessDenied("D-Bus connection is not available")
        try:
            reply = self._bus.call_sync(
                DBUS_NAME,
                DBUS_PATH,
                DBUS_IFACE,
                "GetNameOwner",
                GLib.Variant("(s)", (name,)),
                GLib.VariantType.new("(s)"),
                0,
                LOGIND_TIMEOUT_MS,
                None,
            )
        except GLib.Error as error:
            # ``NameHasNoOwner`` is the normal "adapter not running" answer.
            self._log.debug("no owner for %s: %s", name, error)
            return None
        try:
            return str(reply.unpack()[0])
        except (IndexError, TypeError, ValueError):  # pragma: no cover - defensive
            return None

    def caller_pid(self, sender: str) -> int:
        """Return the pid owning the bus connection ``sender``."""
        if not sender:
            raise AccessDenied("missing sender")
        try:
            values = self._call(
                DBUS_NAME,
                DBUS_PATH,
                DBUS_IFACE,
                "GetConnectionUnixProcessID",
                GLib.Variant("(s)", (sender,)),
                "(u)",
                LOGIND_TIMEOUT_MS,
            )
        except AccessDenied as error:
            self._log.warning("cannot resolve pid of %s: %s", sender, error)
            raise
        try:
            return int(values[0])
        except (IndexError, TypeError, ValueError):  # pragma: no cover - defensive
            raise AccessDenied(f"GetConnectionUnixProcessID returned {values!r}") from None

    def session_identity(self, sender: str) -> tuple[int, int]:
        """Return ``(uid, pid)`` of ``sender``, cached per connection.

        A D-Bus unique name is bound to one connection, so its uid and pid
        cannot change while the connection lives.  Caching them removes two
        synchronous D-Bus calls from every 0.5 s adapter heartbeat.
        """
        cached = self._identity_cache.get(sender)
        if cached is not None:
            return cached
        uid = self.caller_uid(sender)
        pid = self.caller_pid(sender)
        self._identity_cache[sender] = (uid, pid)
        while len(self._identity_cache) > CACHE_LIMIT:
            self._identity_cache.pop(next(iter(self._identity_cache)))
        return uid, pid

    def process_in_session_unit(self, pid: int, uid: int) -> bool:
        """Whether ``pid`` runs inside the adapter's systemd user unit.

        Reads ``/proc/<pid>/cgroup``; an unreadable or missing file fails
        closed because the cgroup membership is part of the adapter identity.
        """
        path = self._proc_root / str(int(pid)) / "cgroup"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            self._log.debug("cannot read cgroup of pid %s", pid)
            return False
        manager = f"/user@{uid}.service/"
        for line in text.splitlines():
            fields = line.split(":", 2)
            if len(fields) != 3:
                continue
            hierarchy, controllers, cgroup = fields
            if not (
                (hierarchy == "0" and not controllers)
                or (hierarchy.isdecimal() and controllers == "name=systemd")
            ) or not cgroup.startswith("/"):
                continue
            # A delegated subtree under another service may contain arbitrary
            # names. Match the unit directly under the user manager or its slices.
            parent, separator, unit_path = cgroup.partition(manager)
            *user_slices, unit = unit_path.split("/")
            system_slices = parent.split("/")[1:]
            if (
                separator
                and unit == self._session_unit
                and all(part.endswith(".slice") for part in system_slices + user_slices)
            ):
                return True
        return False

    # -- session discovery ----------------------------------------------

    def active_session_cached(self, uid: int) -> SessionInfo:
        """Return the active local session of ``uid`` with a <=1 s cache.

        Only positive results are cached: a session that just became inactive
        must be rejected on the next call (the adapter's own heartbeat backoff
        then stops it from hammering logind).
        """
        uid = int(uid)
        now = self._monotonic()
        cached = self._session_cache.get(uid)
        if cached is not None and now - cached[0] <= self._session_cache_seconds:
            return cached[1]
        session = self.active_session(uid)
        self._session_cache[uid] = (now, session)
        while len(self._session_cache) > CACHE_LIMIT:
            self._session_cache.pop(next(iter(self._session_cache)))
        return session

    def verify_session_adapter(self, sender: str) -> int:
        """Authenticate a ``SessionUpdate`` sender; returns its uid.

        Implements the four-step identity chain described in the module
        docstring: caller uid, per-uid adapter name owner, active local
        graphical session (cached <=1 s) and systemd user-unit cgroup.  Every
        failure raises :class:`AccessDenied`; the daemon maps it to
        ``access-denied``.
        """
        uid, pid = self.session_identity(sender)
        expected_name = adapter_name(uid)
        owner = self.session_name_owner(expected_name)
        if owner is None or owner != sender:
            raise AccessDenied(
                f"sender is not the owner of the session adapter name {expected_name}"
            )
        self.active_session_cached(uid)
        if not self.process_in_session_unit(pid, uid):
            raise AccessDenied(
                f"sender process {pid} is not in {self._session_unit}"
            )
        return uid


    def active_session(self, uid: int) -> SessionInfo:
        """Return the active local graphical session for ``uid`` or raise."""
        values = self._call(
            LOGIN1_NAME,
            LOGIN1_PATH,
            LOGIN1_IFACE,
            "ListSessions",
            None,
            "(a(susso))",
            LOGIND_TIMEOUT_MS,
        )
        rows = values[0] if values else []
        for row in rows:
            try:
                session_id, user, _name, seat, path = row
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
            if int(user) != int(uid) or seat != SEAT:
                continue
            session_type = self._session_type(str(path))
            if session_type is not None:
                return SessionInfo(
                    uid=int(user),
                    session_id=str(session_id),
                    path=str(path),
                    seat=str(seat),
                    type=session_type,
                )
        raise AccessDenied(f"no active local graphical session for uid {uid}")

    def _session_type(self, path: str) -> str | None:
        """Return the session type when it is an active local graphical one."""
        values = self._call(
            LOGIN1_NAME,
            path,
            "org.freedesktop.DBus.Properties",
            "GetAll",
            GLib.Variant("(s)", (LOGIN1_SESSION_IFACE,)),
            "(a{sv})",
            LOGIND_TIMEOUT_MS,
        )
        properties = values[0] if values else {}
        if not isinstance(properties, dict):  # pragma: no cover - defensive
            return None
        if not properties.get("Active") or properties.get("Remote"):
            return None
        session_type = properties.get("Type")
        if session_type not in SESSION_TYPES:
            return None
        return str(session_type)

    # -- authorization ---------------------------------------------------

    def _check_polkit(self, sender: str) -> None:
        parameters = GLib.Variant(
            "((sa{sv})sa{ss}us)",
            (
                ("system-bus-name", {"name": GLib.Variant("s", sender)}),
                self._polkit_action,
                {},
                0,
                "",
            ),
        )
        values = self._call(
            POLKIT_NAME,
            POLKIT_PATH,
            POLKIT_IFACE,
            "CheckAuthorization",
            parameters,
            "((bba{ss}))",
            POLKIT_TIMEOUT_MS,
        )
        try:
            authorized = bool(values[0][0])
        except (IndexError, TypeError):  # pragma: no cover - defensive
            authorized = False
        if not authorized:
            raise AccessDenied("polkit authorization denied")

    def authorize(self, sender: str) -> int:
        """Return the uid of an authorized caller, caching success for 2 s.

        Failed authorizations are deliberately *not* cached: a user who just
        unlocked the session must be able to retry immediately.
        """
        now = self._monotonic()
        cached = self._cache.get(sender)
        if cached is not None and now - cached[0] <= self._cache_seconds:
            return cached[1]
        uid = self.caller_uid(sender)
        self.active_session(uid)
        self._check_polkit(sender)
        self._cache[sender] = (now, uid)
        self._prune_cache(now)
        return uid

    def _prune_cache(self, now: float) -> None:
        if len(self._cache) <= CACHE_LIMIT:
            return
        expired = [
            sender
            for sender, (stamp, _uid) in self._cache.items()
            if now - stamp > self._cache_seconds
        ]
        for sender in expired:
            del self._cache[sender]
        while len(self._cache) > CACHE_LIMIT:
            self._cache.pop(next(iter(self._cache)))

    def invalidate(self, sender: str | None = None) -> None:
        """Forget cached authorizations (all of them when ``sender`` is None)."""
        if sender is None:
            self._cache.clear()
            self._identity_cache.clear()
            self._session_cache.clear()
        else:
            self._cache.pop(sender, None)
            self._identity_cache.pop(sender, None)
