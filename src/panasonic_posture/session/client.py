"""D-Bus client for the privileged posture service.

The service exposes a very small API (see ``protocol.DBUS_XML``).  Every
failure is normalised into :class:`ServiceError` with the stable error code
embedded in the D-Bus error message, so callers never have to parse
``GDBus.Error:`` prefixes themselves.
"""

from __future__ import annotations

from collections.abc import Callable

import logging

from gi.repository import Gio, GLib

from .. import model, protocol
from .failures import ErrorCode, ServiceError, describe_error

from .dbus_errors import service_error

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 1800
_GDBUS_PREFIX = "GDBus.Error:"


def _split_dbus_error(message: str) -> tuple[str, str, str]:
    """Split a D-Bus error message into ``(name, code, detail)``."""

    text = message or ""
    name = ""
    if text.startswith(_GDBUS_PREFIX):
        remainder = text[len(_GDBUS_PREFIX):]
        candidate, separator, tail = remainder.partition(":")
        if separator:
            name = candidate.strip()
            text = tail.strip()
        else:
            text = remainder.strip()
    code, detail = protocol.split_error(text)
    return name, code, detail or ""


class SystemClient:
    """Synchronous client for ``work.mmoe.panasonic.Posture``.

    ``connection`` is injectable so tests can use a fake bus without a real
    system bus being available.
    """

    def __init__(self, connection=None, timeout_ms: int = DEFAULT_TIMEOUT_MS,
                 *, screen_context: Callable[[], bool] | None = None):
        self._screen_context = screen_context
        self._connection = connection
        self.timeout_ms = int(timeout_ms)

    @property
    def connection(self):
        if self._connection is None:
            self._connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return self._connection

    def _call(self, method: str, parameters=None):
        try:
            return self.connection.call_sync(
                protocol.NAME,
                protocol.PATH,
                protocol.IFACE,
                method,
                parameters,
                None,
                Gio.DBusCallFlags.NONE,
                self.timeout_ms,
                None,
            )
        except GLib.Error as error:
            name, code, detail = _split_dbus_error(describe_error(error))
            if name:
                log.debug("D-Bus call %s failed: %s: %s", method, name, detail)
            raise service_error(code, detail or None, name=name) from error

    def get_state(self) -> model.StateSnapshot:
        """Return the validated, strictly typed state snapshot."""

        reply = self._call("GetState")
        try:
            payload = reply.unpack()[0]
        except (TypeError, IndexError) as error:
            raise ServiceError(ErrorCode.INTERNAL, "malformed GetState reply") from error
        try:
            return model.parse_state_snapshot(payload)
        except (TypeError, ValueError) as error:
            raise ServiceError(ErrorCode.INTERNAL, f"invalid state document: {error}") from error

    def session_update(
        self,
        generation: int,
        ready: bool,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Publish the adapter result/lease heartbeat (``(u b s s)``)."""

        parameters = GLib.Variant("(ubss)", (int(generation), bool(ready),
                                              str(error_code or ""), str(error_detail or "")))
        if self._screen_context is not None and self._screen_context():
            try:
                self._call("SessionUpdateUnattended", parameters)
                return
            except ServiceError as error:
                if error.name != "org.freedesktop.DBus.Error.UnknownMethod":
                    raise
        self._call("SessionUpdate", parameters)

    def confirm_tent(self, epoch: int) -> None:
        self._call("ConfirmTent", GLib.Variant("(u)", (int(epoch),)))

    def wake_screen(self, generation: int, serial: int) -> None:
        self._call("SessionWake", GLib.Variant("(ut)", (int(generation), int(serial))))

    def set_tent_enabled(self, enabled: bool) -> None:
        self._call("SetTentEnabled", GLib.Variant("(b)", (bool(enabled),)))

    def set_stand_prompt(self, enabled: bool) -> None:
        self._call("SetStandPrompt", GLib.Variant("(b)", (bool(enabled),)))

    def confirm_stand(self, epoch: int) -> None:
        self._call("ConfirmStand", GLib.Variant("(u)", (int(epoch),)))


__all__ = ["DEFAULT_TIMEOUT_MS", "SystemClient"]
