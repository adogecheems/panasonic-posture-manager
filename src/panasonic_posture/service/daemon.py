"""Privileged system D-Bus service entry point.

This module only contains the D-Bus shell: it owns the bus name, dispatches
methods with authorization, installs guarded GLib callbacks (timer, signals,
PrepareForSleep) and delegates lifecycle work to
:class:`~panasonic_posture.service.application.ServiceApplication`.  All
decision and transaction logic lives in
:mod:`panasonic_posture.service.engine` so it is unit testable without root, a
bus or hardware.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import signal
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Sequence

from gi.repository import Gio, GLib

try:  # pragma: no cover - always present on the target platform
    from gi.repository import GLibUnix
except ImportError:  # pragma: no cover - fallback for non-Linux development
    GLibUnix = None  # type: ignore[assignment]

from .. import __version__
from ..model import StateSnapshot
from ..protocol import (
    DBUS_ERROR,
    DBUS_XML,
    IFACE,
    NAME,
    PATH,
    TICK_SECONDS,
    ErrorCode,
    error_message,
)
from ..tasks import thread_executor
from .access import AccessDenied
from .application import ServiceApplication
from .engine import ServiceError, guard
from .settings import DEFAULT_SETTINGS_PATH

LOG = logging.getLogger("panasonic_posture.service.daemon")

LOGIND_NAME = "org.freedesktop.login1"
LOGIND_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER = "org.freedesktop.login1.Manager"
MAX_PENDING_AUTHORIZATIONS = 16


class Daemon:
    """Own the system bus name and translate D-Bus calls into engine actions."""

    def __init__(
        self,
        bus_type: Gio.BusType,
        config_path: str | Path = DEFAULT_SETTINGS_PATH,
    ) -> None:
        self.bus_type = bus_type
        self.development = bus_type == Gio.BusType.SESSION
        self.config_path = Path(config_path)
        self.stop = threading.Event()
        self._exit_code = 0
        self._name_acquired_ok = False
        self.connection: Any = None
        self.registration: Any = None
        self.owner: int | None = None
        self._timeout_id: int | None = None
        self._tick_idle_id: int | None = None
        self._sleep_subscription: int | None = None
        self._signal_sources: list[int] = []
        self.loop = GLib.MainLoop()
        # Separate workers keep a slow polkit request from delaying lease
        # heartbeats. Each owns the corresponding AccessControl caches.
        self._authorization_workers = {
            "request": thread_executor("posture-request-auth"),
            "session": thread_executor("posture-session-auth"),
        }
        self._authorization_pending = {"request": 0, "session": 0}

        self.application = ServiceApplication.create(
            stop=self.stop,
            config_path=self.config_path,
            development=self.development,
            instance=secrets.token_hex(8),
            on_state=self._emit_state,
        )

    # -- GLib callbacks --------------------------------------------------

    def _tick(self) -> bool:
        self.application.engine.tick(time.monotonic())
        return True

    def _schedule_tick(self) -> None:
        """Apply a D-Bus mutation after the method reply has been queued.

        A tick can perform bounded but slow relay cleanup or display work.
        Running it directly from the D-Bus callback made a successful request
        wait for that work (occasionally for more than a second).  Coalescing
        the follow-up work on a high-priority idle source keeps the control UI
        responsive without changing the target/result ordering; it also runs
        before queued D-Bus reads, so GetState cannot observe a bumped
        generation with the previous target.
        """
        if self._tick_idle_id is not None:
            return
        try:
            self._tick_idle_id = GLib.idle_add(
                self._run_scheduled_tick, priority=GLib.PRIORITY_HIGH
            )
        except Exception:  # pragma: no cover - GLib shutdown race
            LOG.exception("cannot schedule service tick")

    def _run_scheduled_tick(self) -> bool:
        self._tick_idle_id = None
        try:
            self._tick()
        except Exception:  # pragma: no cover - guarded callback boundary
            LOG.exception("scheduled service tick failed")
        return GLib.SOURCE_REMOVE

    def _emit_state(self, state: StateSnapshot) -> None:
        if self.connection is None:
            return
        try:
            self.connection.emit_signal(
                None,
                PATH,
                IFACE,
                "StateChanged",
                GLib.Variant("(s)", (self.application.engine.state_json(),)),
            )
        except GLib.Error as error:
            LOG.warning("cannot emit StateChanged: %s", error)

    def _name_acquired(self, _connection: Any, name: str) -> None:
        self._name_acquired_ok = True
        LOG.info("owning %s", name)

    def _name_lost(self, connection: Any, name: str) -> None:
        duplicate = False
        if not self._name_acquired_ok and not self._exit_code and connection is not None:
            try:
                reply = connection.call_sync(
                    "org.freedesktop.DBus",
                    "/org/freedesktop/DBus",
                    "org.freedesktop.DBus",
                    "NameHasOwner",
                    GLib.Variant("(s)", (name,)),
                    GLib.VariantType.new("(b)"),
                    Gio.DBusCallFlags.NONE,
                    500,
                    None,
                )
                duplicate = reply.unpack()[0] is True
            except GLib.Error:
                LOG.warning("cannot check ownership of bus name %s", name, exc_info=True)
        if self._name_acquired_ok:
            # We served requests and then lost the name: restarting is the only
            # way back, so let systemd (Restart=on-failure) do it.
            LOG.error("lost bus name %s; exiting with failure", name)
            self._exit_code = 1
        elif duplicate:
            # Only a confirmed duplicate is a successful early exit.
            LOG.info("bus name %s is already owned; exiting", name)
            self._exit_code = 0
        else:
            LOG.error("cannot acquire bus name %s; exiting with failure", name)
            self._exit_code = 1
        self.loop.quit()

    def _bus_acquired(self, connection: Any, _name: str) -> None:
        try:
            self.connection = connection
            self.application.access.attach(connection)
            info = Gio.DBusNodeInfo.new_for_xml(DBUS_XML).interfaces[0]
            self.registration = connection.register_object(
                PATH, info, self._method, None, None
            )
            if not self.development:
                self._subscribe_sleep(connection)
            self.application.start()
            self._tick()
        except Exception:  # noqa: BLE001 - half-initialised service must not linger
            LOG.exception("bus acquisition failed; stopping")
            self._exit_code = 1
            self.loop.quit()

    def _subscribe_sleep(self, connection: Any) -> None:
        try:
            self._sleep_subscription = connection.signal_subscribe(
                LOGIND_NAME,
                LOGIND_MANAGER,
                "PrepareForSleep",
                LOGIND_PATH,
                None,
                Gio.DBusSignalFlags.NONE,
                guard(self._sleep_event, default=None),
            )
        except GLib.Error as error:  # pragma: no cover - bus already gone
            LOG.warning("cannot subscribe to PrepareForSleep: %s", error)

    def _sleep_event(
        self,
        _connection: Any,
        _sender: str,
        _path: str,
        _interface: str,
        _signal: str,
        parameters: GLib.Variant,
    ) -> None:
        try:
            suspended = bool(parameters.unpack()[0])
        except (TypeError, IndexError):  # pragma: no cover - malformed signal
            LOG.warning("ignoring malformed PrepareForSleep payload")
            return
        LOG.info("PrepareForSleep(%s)", suspended)
        # Device nodes may be removed/recreated across suspend.  Force a full
        # discovery on the first post-resume tick instead of trusting a cached
        # event number.
        self.application.sleep_event(suspended)
        self._schedule_tick()

    def _quit(self, *_args: Any) -> bool:
        LOG.info("shutdown signal received")
        self.loop.quit()
        # Keep the signal source installed: the explicit ``source_remove`` in
        # shutdown() is then the only owner and GLib does not warn about a
        # source that was already auto-removed after firing.
        return True  # GLib.SOURCE_CONTINUE

    # -- authorization / dispatch ---------------------------------------

    def _method(
        self,
        _connection: Any,
        sender: str,
        _path: str,
        _interface: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Any,
    ) -> None:
        # The development bus has no production identity checks.
        if method == "GetState" or self.development:
            self._dispatch_method(sender, method, parameters, invocation)
            return
        access = self.application.access
        if method == "SessionUpdate":
            lane, authorize = "session", access.verify_session_adapter
        else:
            lane, authorize = "request", access.authorize
        if self._authorization_pending[lane] >= MAX_PENDING_AUTHORIZATIONS:
            invocation.return_dbus_error(
                DBUS_ERROR, error_message(ErrorCode.INTERNAL, "authorization-busy")
            )
            return
        try:
            future = self._authorization_workers[lane].submit(authorize, sender)
        except RuntimeError:
            LOG.exception("cannot schedule authorization for %s", method)
            invocation.return_dbus_error(
                DBUS_ERROR, error_message(ErrorCode.INTERNAL, "method-failed")
            )
            return
        self._authorization_pending[lane] += 1

        def dispatch(result: Future[int]) -> bool:
            self._authorization_pending[lane] -= 1
            if not self.stop.is_set():
                self._dispatch_method(sender, method, parameters, invocation, result)
            return GLib.SOURCE_REMOVE

        def authorized(result: Future[int]) -> None:
            if not self.stop.is_set():
                GLib.idle_add(dispatch, result, priority=GLib.PRIORITY_DEFAULT)

        future.add_done_callback(authorized)

    def _dispatch_method(
        self,
        sender: str,
        method: str,
        parameters: GLib.Variant,
        invocation: Any,
        authorization: Future[int] | None = None,
    ) -> None:
        try:
            arguments = parameters.unpack()
            if method == "GetState":
                # Read-only cached snapshot: never run a full transaction from
                # an unauthenticated, high-frequency polling call (review Q-06).
                invocation.return_value(
                    GLib.Variant("(s)", (self.application.engine.state_json(),))
                )
                return
            if authorization is None and not self.development:
                raise AccessDenied("missing method authorization")
            uid = authorization.result() if authorization is not None else 0
            if method == "SetMode":
                self.application.engine.set_mode(str(arguments[0]))
            elif method == "Retry":
                self.application.engine.retry()
            elif method == "SetTentEnabled":
                self.application.engine.set_tent_enabled(bool(arguments[0]))
            elif method == "SetTentPrompt":
                self.application.engine.set_tent_prompt(bool(arguments[0]))
            elif method == "SetStandPrompt":
                self.application.engine.set_stand_prompt(bool(arguments[0]))
            elif method == "CalibrateSensor":
                self.application.engine.calibrate_sensor()
            elif method == "ResetSensorCalibration":
                self.application.engine.reset_sensor_calibration()
            elif method == "ConfirmTent":
                self.application.engine.confirm_tent(int(arguments[0]))
            elif method == "ConfirmStand":
                self.application.engine.confirm_stand(int(arguments[0]))
            elif method == "SessionUpdate":
                generation, ready, error_code, error_detail = arguments
                self.application.engine.session_update(
                    int(generation), bool(ready), str(error_code), str(error_detail), uid
                )
            else:
                raise ServiceError(ErrorCode.INTERNAL, "unknown-method")
            self._schedule_tick()
            invocation.return_value(GLib.Variant("()", ()))
        except ServiceError as error:
            LOG.debug("method %s rejected: %s", method, error)
            invocation.return_dbus_error(DBUS_ERROR, str(error))
        except AccessDenied as error:
            LOG.warning("access denied for %s from %s: %s", method, sender, error)
            invocation.return_dbus_error(
                DBUS_ERROR, error_message(ErrorCode.ACCESS_DENIED, str(error))
            )
        except (GLib.Error, OSError, ValueError) as error:
            LOG.warning("method %s failed: %s", method, error)
            invocation.return_dbus_error(
                DBUS_ERROR, error_message(ErrorCode.INTERNAL, str(error))
            )
        except Exception:  # noqa: BLE001 - never leave a D-Bus call unanswered
            LOG.exception("method %s crashed", method)
            invocation.return_dbus_error(
                DBUS_ERROR, error_message(ErrorCode.INTERNAL, "method-failed")
            )

    # -- lifecycle -------------------------------------------------------

    def run(self) -> int:
        """Run the GLib main loop until termination; return an exit code."""
        self.owner = Gio.bus_own_name(
            self.bus_type,
            NAME,
            Gio.BusNameOwnerFlags.NONE,
            guard(self._bus_acquired, default=None),
            guard(self._name_acquired, default=None),
            guard(self._name_lost, default=None),
        )
        self._timeout_id = GLib.timeout_add(
            max(1, int(TICK_SECONDS * 1000)), guard(self._tick)
        )
        for signum in (signal.SIGTERM, signal.SIGINT):
            if GLibUnix is not None:
                try:
                    source = GLibUnix.signal_add(
                        GLib.PRIORITY_DEFAULT, signum, guard(self._quit)
                    )
                    self._signal_sources.append(source)
                except (TypeError, ValueError):  # pragma: no cover - unusual build
                    pass
        try:
            self.loop.run()
        finally:
            self.shutdown()
        return self._exit_code

    def shutdown(self) -> None:
        LOG.info("stopping service")
        self.stop.set()
        for worker in self._authorization_workers.values():
            worker.shutdown(wait=False, cancel_futures=True)
        if self._timeout_id is not None:
            try:
                GLib.source_remove(self._timeout_id)
            except (TypeError, ValueError):  # pragma: no cover - already gone
                pass
            self._timeout_id = None
        if self._tick_idle_id is not None:
            try:
                GLib.source_remove(self._tick_idle_id)
            except (TypeError, ValueError):  # pragma: no cover - already gone
                pass
            self._tick_idle_id = None
        for source in self._signal_sources:
            try:
                GLib.source_remove(source)
            except (TypeError, ValueError):  # pragma: no cover - already gone
                pass
        self._signal_sources.clear()
        self.application.close()
        if self.connection is not None:
            if self._sleep_subscription is not None:
                try:
                    self.connection.signal_unsubscribe(self._sleep_subscription)
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    pass
                self._sleep_subscription = None
            if self.registration is not None:
                try:
                    self.connection.unregister_object(self.registration)
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    pass
                self.registration = None
        if self.owner is not None:
            Gio.bus_unown_name(self.owner)
            self.owner = None
        self.connection = None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="panasonic-posture-manager-service",
        description="Panasonic posture manager privileged service (internal entry point).",
    )
    parser.add_argument(
        "--session-bus",
        action="store_true",
        help="attach to the session bus instead of the system bus (development only)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_SETTINGS_PATH,
        metavar="PATH",
        help=f"settings file (default: {DEFAULT_SETTINGS_PATH})",
    )
    parser.add_argument("--version", action="version", version=__version__)
    arguments = parser.parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    bus_type = Gio.BusType.SESSION if arguments.session_bus else Gio.BusType.SYSTEM
    try:
        return Daemon(bus_type, arguments.config).run()
    except KeyboardInterrupt:  # pragma: no cover - interactive development
        return 0
    except Exception:  # pragma: no cover - top-level process boundary
        LOG.exception("service terminated with an unexpected error")
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
