"""Unprivileged desktop session adapter process shell.

Responsibilities:

* own the per-uid system-bus name **before** touching the display or the first
  transaction, which makes the adapter a single instance per user and lets the
  daemon authenticate it (P2-13);
* install the 100 ms GLib tick and exception guard, then delegate display,
  lease and notification reconciliation to :class:`runtime.SessionRuntime`;
* subscribe to notification actions and forward them to the runtime (P2-15);
* on SIGTERM close the notification, restore the display and release the bus
  name.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from concurrent.futures import Executor
from functools import partial

from gi.repository import Gio, GLib

from .. import protocol
from . import errors
from .client import SystemClient
from .desktop import create_display
from .errors import describe_error
from .journal import default_path
from .lockstate import query_locked
from .notify import NOTIFY_DEST, NOTIFY_IFACE, NOTIFY_PATH, Notifier
from .runtime import SessionRuntime

log = logging.getLogger(__name__)

try:  # pragma: no cover - GLibUnix is only needed for the real process
    from gi.repository import GLibUnix
except (ImportError, ValueError):  # pragma: no cover - old PyGObject
    GLibUnix = None

STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class Agent:
    """Own the adapter's bus identity and GLib process lifecycle.

    Per-tick state lives in :class:`SessionRuntime`; this shell only decides
    when it is safe to construct and start that runtime.  Every collaborator is
    injectable so tests can drive the process with fake connections, clocks and
    display/notifier objects:

    * ``client``: :class:`client.SystemClient` compatible object;
    * ``build``: callable invoked after the bus name was acquired; it must
      attach ``client``/``display``/``notifier`` (default builds the real
      objects);
    * ``own_name``/``unown_name``/``loop_factory``/``timeout_add``/``signal_add``:
      GLib plumbing;
    * ``screen_locked``: callable returning the screen-lock state.
    """

    TICK_MS = int(protocol.TICK_SECONDS * 1000)

    def __init__(
        self,
        system_bus=None,
        session_bus=None,
        *,
        client: SystemClient | None = None,
        uid: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        translate: Callable[[str], str] | None = None,
        build: Callable[[Agent], None] | None = None,
        own_name: Callable | None = None,
        unown_name: Callable | None = None,
        loop_factory: Callable | None = None,
        timeout_add: Callable | None = None,
        signal_add: Callable | None = None,
        screen_locked: Callable[[], bool] | None = None,
        journal_path=None,
        executor: Executor | None = None,
        install_signals: bool = True,
    ):
        self._system_bus = system_bus
        self._session_bus = session_bus
        self._client = client
        self.uid = os.getuid() if uid is None else int(uid)
        #: Per-uid system bus name owned by this adapter (D-Bus identity).
        self.session_name = protocol.session_name(self.uid)
        self._clock = clock
        self._ = translate or errors.get_translator()
        self._build = build
        self._own_name = own_name or Gio.bus_own_name_on_connection
        self._unown_name = unown_name or Gio.bus_unown_name
        self._loop_factory = loop_factory or GLib.MainLoop
        self._timeout_add = timeout_add or GLib.timeout_add
        self._signal_add = signal_add or (
            GLibUnix.signal_add if (install_signals and GLibUnix is not None) else None
        )
        self._screen_locked = screen_locked
        self._journal_path = journal_path
        self._executor = executor

        self.client: SystemClient | None = None
        self.display = None
        self.notifier: Notifier | None = None
        self.runtime: SessionRuntime | None = None

        self.loop = None
        self.owner = None
        self._timer = None
        self._action_subscription = None
        self._signal_sources: list = []
        self._acquired = False
        self._name_lost = False
        self._failed = False
        self._stopping = False

    # -- properties -------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Whether the current display generation was applied (test/read API)."""
        return bool(self.runtime and self.runtime.ready)

    @property
    def locked(self) -> bool:
        """Last observed screen-lock state (test/read API)."""
        return bool(self.runtime and self.runtime.locked)

    @property
    def _failure_active(self) -> bool:
        """Compatibility view of the runtime's lease-failure state."""
        return bool(self.runtime and self.runtime.failure_active)

    @property
    def _failure_retry_at(self) -> float:
        """Compatibility view of the runtime's lease retry deadline."""
        return 0.0 if self.runtime is None else self.runtime.failure_retry_at

    @property
    def system_bus(self):
        if self._system_bus is None:
            self._system_bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return self._system_bus

    @property
    def session_bus(self):
        if self._session_bus is None:
            self._session_bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        return self._session_bus

    # -- process lifecycle ------------------------------------------------

    def serve(self) -> int:
        """Acquire the bus name, run the main loop and clean up.

        Returns ``0`` for a normal shutdown and for a duplicate instance
        (another adapter already owns the name; systemd must not restart us),
        ``1`` when the name was acquired and then lost or initialization
        failed.
        """

        loop = self.loop = self._loop_factory()
        for signum in STOP_SIGNALS:
            if self._signal_add is None:
                break
            try:
                self._signal_sources.append(
                    self._signal_add(GLib.PRIORITY_DEFAULT, signum, self._on_signal)
                )
            except Exception:  # noqa: BLE001 - injected GLib callback
                log.debug("cannot install handler for signal %s", signum)
        try:
            self.owner = self._own_name(
                self.system_bus,
                self.session_name,
                Gio.BusNameOwnerFlags.NONE,
                self._on_name_acquired,
                self._on_name_lost,
            )
        except (GLib.Error, OSError) as error:
            log.error(
                "cannot contact the system bus for %s: %s",
                self.session_name,
                describe_error(error),
            )
            self._cleanup()
            return 1
        if not self._name_lost:
            loop.run()
        self._cleanup()
        return 1 if self._failed else 0

    def _on_signal(self, *_args) -> bool:
        log.info("session adapter stopping on signal")
        self._stopping = True
        self._quit()
        return False

    def _quit(self) -> None:
        if self.loop is not None:
            try:
                self.loop.quit()
            except Exception:  # pragma: no cover - loop already gone
                log.debug("main loop quit failed", exc_info=True)

    def _on_name_acquired(self, _connection=None, _name=None) -> None:
        self._acquired = True
        try:
            self._build_components()
        except Exception:
            log.exception("cannot initialise the desktop session adapter")
            self._acquired = False
            self._failed = True
            self._quit()
            return
        if self._timeout_add is not None and not self._stopping:
            try:
                self._timer = self._timeout_add(self.TICK_MS, self._tick)
            except Exception:  # pragma: no cover
                log.exception("cannot start the session adapter tick timer")
                self._quit()
                return
        self._tick()  # the name is already owned: run the first transaction now

    def _on_name_lost(self, _connection=None, _name=None) -> None:
        if self._acquired:
            log.error("lost the system bus name %s; exiting", self.session_name)
            self._failed = True
        else:
            owner = self._name_owner()
            if owner is None:
                # The name was not taken: the bus policy does not allow this
                # uid to own it (or the connection vanished).  Fail so systemd
                # and the unit state show the misconfiguration.
                log.error(
                    "cannot own the system bus name %s; "
                    "check the D-Bus policy for the session adapter",
                    self.session_name,
                )
                self._failed = True
            else:
                owner_uid = self._connection_uid(owner)
                if owner_uid == self.uid:
                    # A second adapter for the same user: single instance wins,
                    # exit 0 so systemd does not restart it.
                    log.info(
                        "another session adapter for uid %s already owns %s; exiting",
                        self.uid,
                        self.session_name,
                    )
                else:
                    # Another user grabbed our per-uid name: exiting 0 would
                    # leave this user without an adapter forever, so fail and
                    # let systemd retry with backoff (P2 multi-user).
                    log.error(
                        "system bus name %s is owned by uid %s instead of %s; "
                        "exiting so the unit can retry",
                        self.session_name,
                        owner_uid if owner_uid is not None else "unknown",
                        self.uid,
                    )
                    self._failed = True
        self._name_lost = True
        self._quit()

    def _name_owner(self) -> str | None:
        """Return the owner unique name of our per-uid bus name, if any."""

        try:
            reply = self.system_bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetNameOwner",
                GLib.Variant("(s)", (self.session_name,)),
                GLib.VariantType.new("(s)"),
                Gio.DBusCallFlags.NONE,
                500,
                None,
            )
        except (GLib.Error, AttributeError, OSError, TypeError):
            return None
        try:
            return str(reply.unpack()[0] or "") or None
        except (TypeError, IndexError):  # pragma: no cover - malformed reply
            return None

    def _connection_uid(self, owner: str) -> int | None:
        """Map a unique bus name to its uid (``GetConnectionUnixUser``)."""

        try:
            reply = self.system_bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "GetConnectionUnixUser",
                GLib.Variant("(s)", (owner,)),
                GLib.VariantType.new("(u)"),
                Gio.DBusCallFlags.NONE,
                500,
                None,
            )
        except (GLib.Error, AttributeError, OSError, TypeError):
            return None
        try:
            return int(reply.unpack()[0])
        except (TypeError, ValueError, IndexError):  # pragma: no cover
            return None

    # -- construction after the name was acquired -------------------------

    def attach(self, client, display, notifier) -> None:
        """Attach the runtime collaborators (used by the default build and tests)."""

        self.client = client
        self.display = display
        self.notifier = notifier
        screen_locked = self._screen_locked
        if screen_locked is None:
            screen_locked = partial(
                query_locked, system_bus=self._system_bus, session_bus=self._session_bus
            )
        self.runtime = SessionRuntime(
            client,
            display,
            notifier,
            screen_locked=screen_locked,
            adapter_errors=(GLib.Error,),
            executor=self._executor,
        )

    def _build_components(self) -> None:
        if self._build is not None:
            self._build(self)
        else:
            client = self._client or SystemClient(self.system_bus)
            display = create_display(
                self.session_bus,
                self._journal_path or default_path(),
                self._,
            )
            notifier = Notifier(self.session_bus, self._)
            self.attach(client, display, notifier)
            self._subscribe_actions()
        if any(item is None for item in (self.client, self.display, self.notifier)):
            raise RuntimeError("session adapter components are not attached")
        if self.runtime is None:
            self.attach(self.client, self.display, self.notifier)

    def _subscribe_actions(self) -> None:
        try:
            self._action_subscription = self.session_bus.signal_subscribe(
                NOTIFY_DEST,
                NOTIFY_IFACE,
                "ActionInvoked",
                NOTIFY_PATH,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_action,
            )
        except (AttributeError, GLib.Error) as error:
            log.warning(
                "cannot subscribe to notification actions: %s", describe_error(error)
            )

    # -- periodic work ----------------------------------------------------

    def _tick(self) -> bool:
        """GLib timeout callback: never let an exception kill the source."""

        try:
            self.tick()
        except Exception:
            log.exception("session adapter tick failed")
        return GLib.SOURCE_CONTINUE

    def tick(self, now: float | None = None) -> None:
        if self.runtime is None:
            return
        self.runtime.tick(self._clock() if now is None else now)

    # -- notification actions --------------------------------------------

    def _on_action(self, _bus, _sender, _path, _iface, _signal, parameters) -> None:
        if self.runtime is None:
            return
        try:
            try:
                notification, action = parameters.unpack()
            except (TypeError, ValueError):
                return
            self.runtime.handle_action(notification, action)
        except Exception:
            log.exception("notification action failed")

    # -- shutdown ---------------------------------------------------------

    def _cleanup(self) -> None:
        if self._timer is not None:
            try:
                GLib.source_remove(self._timer)
            except Exception:  # pragma: no cover - source already gone
                log.debug("main loop source already removed", exc_info=True)
            self._timer = None
        if self._action_subscription is not None and self._session_bus is not None:
            try:
                self._session_bus.signal_unsubscribe(self._action_subscription)
            except (AttributeError, GLib.Error):
                pass
            self._action_subscription = None
        if self.runtime is not None:
            self.runtime.close()
        if self.owner is not None:
            try:
                self._unown_name(self.owner)
            except (AttributeError, GLib.Error):  # pragma: no cover - defensive
                pass
            self.owner = None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    del argv  # the adapter only reads the process environment
    try:
        return Agent().serve()
    except Exception:
        log.exception("session adapter failed")
        return 1


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
