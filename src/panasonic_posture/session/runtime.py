"""Per-tick session state machine for the unprivileged adapter.

The process shell in :mod:`panasonic_posture.session.agent` owns bus names,
signals and GLib lifecycle.  This module owns everything that happens while the
adapter is attached to the service: mirroring generations, applying display
transactions, reporting the lease heartbeat, retrying transient failures,
checking the screen lock and synchronising the tent notification.

Keeping the runtime separate makes the failure/retry state machine directly
testable and prevents the D-Bus process shell from accumulating display and
notification policy again.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from gi.repository import Gio, GLib

from .. import protocol
from ..model import Target
from . import errors
from .errors import DisplayError, ErrorCode, ServiceError, describe_error

log = logging.getLogger(__name__)

NOTIFY_ACTIONS = frozenset({"default", "tent"})

#: Error codes that only the session adapter itself can produce.  The
#: privileged service may echo the last SessionUpdate report in the state
#: document; such echoes must not be mistaken for a backend failure (which
#: would stop the display retry loop and restore the display).
_ECHO_ERROR_CODES = frozenset(
    {
        ErrorCode.DISPLAY_MIRRORED,
        ErrorCode.DISPLAY_LAYOUT,
        ErrorCode.DISPLAY_TRANSFORM,
        ErrorCode.DISPLAY_LOCK,
        ErrorCode.DISPLAY_UNAVAILABLE,
        ErrorCode.DISPLAY_UNKNOWN_MODE,
        ErrorCode.DISPLAY_IDENTITY,
    }
)

#: D-Bus error names that mean "identity/lease rejected", not a transient
#: transport failure.  They must stop display transactions immediately (P3).
AUTH_ERROR_NAMES = frozenset(
    {
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.AuthFailed",
        "org.freedesktop.DBus.Error.NotAuthorized",
    }
)

#: Error detail markers used by the service for the same class of rejection.
AUTH_ERROR_DETAIL_MARKERS = (
    "access-denied",
    "not-session-adapter",
    "session-lease-busy",
)

#: Delay before retrying after a transient transport failure (the hard grace
#: before the display is restored is ``protocol.AGENT_FAILURE_GRACE_SECONDS``).
TRANSIENT_RETRY_SECONDS = 0.5


class SessionRuntime:
    """Reconcile one session adapter client with the daemon and the desktop."""

    def __init__(
        self,
        client: Any,
        display: Any,
        notifier: Any,
        *,
        session_bus: Any = None,
        screen_locked: Callable[[], bool] | None = None,
    ) -> None:
        self.client = client
        self.display = display
        self.notifier = notifier
        self._session_bus = session_bus
        self._screen_locked = screen_locked

        # daemon state mirror
        self.instance: str | None = None
        self.generation: int | None = None
        self.ready = False
        self.error_code = ""
        self.error_detail = ""
        self.locked = False
        self.last_feedback: tuple | None = None
        self.last_feedback_at = float("-inf")
        self._last_lock_check = float("-inf")

        # display retry (transient Mutter/GSettings failures)
        self.apply_now = False
        self.retry_display = False
        self.retry_at = 0.0
        self.retry_delay = 0.0

        # lease/authorization failure mode
        self.failure_active = False
        self.failure_count = 0
        self.failure_retry_at = 0.0
        self._failure_since: float | None = None
        self._probe_generation = 0
        self._reported_error: str | None = None

        # transient transport failure grace (a single NoReply must not restore)
        self._transient_since: float | None = None
        self._transient_retry_at = 0.0

    # -- public tick -----------------------------------------------------

    def tick(self, now: float) -> None:
        """Run one reconciliation step after the service state was fetched."""
        if self.failure_active:
            if now < self.failure_retry_at:
                return
            if not self._probe_session(now):
                return
        elif (
            self._transient_since is not None
            and now < self._transient_retry_at
            and now - self._transient_since
            < protocol.AGENT_FAILURE_GRACE_SECONDS
        ):
            # A transient transport failure is being retried: keep the display
            # as it is until either the call succeeds or the grace expires.
            return
        try:
            state = self.client.get_state()
        except (ServiceError, GLib.Error, OSError, ValueError) as error:
            self._handle_service_failure(now, error)
            return
        self._handle_state(state, now)

    def close(self) -> None:
        """Close the notification and restore the display, best effort."""
        self._close_notifier()
        self._restore_display()

    # -- lease recovery --------------------------------------------------

    def _probe_session(self, now: float) -> bool:
        """Probe for lease recovery with a cheap SessionUpdate call."""
        try:
            self.client.session_update(self._probe_generation, False, "", "")
        except (ServiceError, GLib.Error, OSError, ValueError) as error:
            self._enter_failure(now, error)
            return False
        log.info("session adapter lease restored")
        self._reset_transient()
        self.failure_active = False
        self.failure_count = 0
        self.failure_retry_at = 0.0
        self._failure_since = None
        self.generation = None  # force a fresh transaction from GetState
        self.ready = False
        self.error_code = ""
        self.error_detail = ""
        self.apply_now = True
        self.last_feedback = None
        return True

    def _enter_failure(self, now: float, error: BaseException) -> None:
        """Stop display transactions after an authorization/lease failure."""
        self._reset_transient()
        if not self.failure_active:
            self.failure_active = True
            self._failure_since = now
            message = describe_error(error)
            if message != self._reported_error:
                log.warning("session adapter lease failure: %s", message)
                self._reported_error = message
            self.ready = False
            self.error_code = ""
            self.error_detail = ""
            self.apply_now = False
            self.retry_display = False
            self.last_feedback = None
            self._probe_generation = self.generation or 0
            self.generation = None
            self._close_notifier()
            self._restore_display()
        self.failure_count += 1
        delay = min(
            protocol.DISPLAY_RETRY_MAX_SECONDS,
            protocol.DISPLAY_RETRY_BASE_SECONDS
            * (2 ** (self.failure_count - 1)),
        )
        self.failure_retry_at = now + delay

    # -- transient vs. rejected failures (P3) ----------------------------

    def _reset_transient(self) -> None:
        self._transient_since = None
        self._transient_retry_at = 0.0

    @staticmethod
    def _is_lease_rejection(error: BaseException) -> bool:
        """True when the service rejected our identity/lease (not a timeout).

        ``ACCESS_DENIED`` (and the D-Bus authorization error names) mean the
        adapter is no longer allowed to act: display transactions stop at once.
        Everything else (NoReply, timeouts, ServiceUnknown) is a transient
        transport failure and gets a grace period instead of an immediate
        display restore (0.2.x behaved the same way).
        """
        if isinstance(error, ServiceError):
            if error.code == ErrorCode.ACCESS_DENIED:
                return True
            if error.name in AUTH_ERROR_NAMES:
                return True
            detail = error.detail or ""
            if any(marker in detail for marker in AUTH_ERROR_DETAIL_MARKERS):
                return True
        return False

    def _handle_service_failure(self, now: float, error: BaseException) -> None:
        if self._is_lease_rejection(error):
            self._reset_transient()
            self._enter_failure(now, error)
            return
        if self._transient_since is None:
            self._transient_since = now
            log.warning(
                "transient session service failure (grace %ss): %s",
                protocol.AGENT_FAILURE_GRACE_SECONDS,
                describe_error(error),
            )
        if now - self._transient_since >= protocol.AGENT_FAILURE_GRACE_SECONDS:
            self._reset_transient()
            self._enter_failure(now, error)
            return
        self._transient_retry_at = now + TRANSIENT_RETRY_SECONDS

    # -- state reconciliation -------------------------------------------

    def _handle_state(self, state: dict, now: float) -> None:
        instance = state.get("instance")
        if instance != self.instance:
            # The daemon restarted: policy epochs and generations start over.
            self.instance = instance
            self.generation = None
            self.error_code = ""
            self.error_detail = ""
            self.last_feedback = None
            self._reset_display_retry()
            self._close_notifier()
        try:
            generation = int(state.get("generation") or 0)
        except (TypeError, ValueError):
            self._enter_failure(now, ValueError("invalid generation"))
            return
        if generation != self.generation:
            self.generation = generation
            self.ready = False
            self.error_code = ""
            self.error_detail = ""
            self.apply_now = True
            self._reset_display_retry()
            self._reported_error = None

        daemon_error = self._daemon_error(state)
        if daemon_error:
            # A failed privileged backend must not keep a rotated display: undo
            # our transaction and wait until the backend recovers.
            if self.ready:
                self._restore_display()
                self.ready = False
            self.apply_now = False
            self.retry_display = False
        else:
            if not self.ready and not self.apply_now and not self.retry_display:
                self.apply_now = True  # backend recovered (or first sight)
            self._maybe_apply(state, generation, now)

        if not self._send_feedback(generation, now):
            return
        self._maybe_check_lock(now)
        self._maybe_notify(state, now, daemon_error)

    def _daemon_error(self, state: dict) -> str:
        """Return a *backend* error code, ignoring echoes of our own report.

        The state document carries the last failure of the whole pipeline; the
        privileged service may therefore echo the error code we just reported
        through ``SessionUpdate``.  That echo must not disable the local display
        retry loop, so it is filtered out when it matches our own report for the
        current generation.
        """
        code = str(state.get("error_code") or "")
        if not code:
            return ""
        if code in _ECHO_ERROR_CODES or code == self.error_code:
            # Only the session adapter reports display-* codes, and the
            # current code is an echo of our last report: keep retrying.
            return ""
        return code

    def _maybe_apply(self, state: dict, generation: int, now: float) -> bool:
        if self.ready:
            return False
        if not (self.apply_now or (self.retry_display and now >= self.retry_at)):
            return False
        try:
            target = Target.from_dict(state.get("target") or {})
        except (TypeError, ValueError) as error:
            self._fail_display(ErrorCode.INTERNAL, describe_error(error), now)
            return False
        try:
            self.display.apply(target)
        except DisplayError as error:
            self._fail_display(error.code, error.detail, now)
            return False
        except (GLib.Error, OSError, ValueError, errors.SessionError) as error:
            self._fail_display(ErrorCode.INTERNAL, describe_error(error), now)
            return False
        # Guard against a generation change that happened while applying: the
        # newer target must win instead of a stale result being reported.
        try:
            latest = self.client.get_state()
        except (ServiceError, GLib.Error, OSError, ValueError) as error:
            self._handle_service_failure(now, error)
            return False
        try:
            latest_generation = int(latest.get("generation") or 0)
        except (TypeError, ValueError):
            latest_generation = generation
        if latest_generation != generation:
            return False
        self.ready = True
        self.error_code = ""
        self.error_detail = ""
        self.apply_now = False
        self._reset_display_retry()
        return True

    def _fail_display(
        self, code: str | None, detail: str | None, now: float
    ) -> None:
        self.ready = False
        self.error_code = str(code or ErrorCode.INTERNAL)
        self.error_detail = str(detail or "")[:256]
        self.apply_now = False
        self.retry_display = True
        if not self.retry_delay:
            self.retry_delay = protocol.DISPLAY_RETRY_BASE_SECONDS
        else:
            self.retry_delay = min(
                protocol.DISPLAY_RETRY_MAX_SECONDS, self.retry_delay * 2
            )
        self.retry_at = now + self.retry_delay
        log.warning(
            "display transaction failed (%s): %s",
            self.error_code,
            self.error_detail,
        )

    def _send_feedback(self, generation: int, now: float) -> bool:
        feedback = (
            int(generation),
            bool(self.ready),
            self.error_code or "",
            self.error_detail or "",
        )
        if (
            feedback == self.last_feedback
            and now - self.last_feedback_at < protocol.AGENT_HEARTBEAT_SECONDS
        ):
            return True
        try:
            self.client.session_update(*feedback)
        except (ServiceError, GLib.Error, OSError, ValueError) as error:
            self._handle_service_failure(now, error)
            return False
        self.last_feedback = feedback
        self.last_feedback_at = now
        self._reported_error = None
        self._reset_transient()
        return True

    # -- screen lock / notification --------------------------------------

    def _maybe_check_lock(self, now: float) -> None:
        if now - self._last_lock_check < protocol.SESSION_CHECK_SECONDS:
            return
        self._last_lock_check = now
        if self._screen_locked is not None:
            try:
                self.locked = bool(self._screen_locked())
            except (GLib.Error, OSError, ValueError):
                self.locked = False
            return
        if self._session_bus is None:
            self.locked = False
            return
        try:
            reply = self._session_bus.call_sync(
                "org.gnome.ScreenSaver",
                "/org/gnome/ScreenSaver",
                "org.gnome.ScreenSaver",
                "GetActive",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                700,
                None,
            )
            self.locked = bool(reply.unpack()[0])
        except (GLib.Error, OSError, ValueError, AttributeError, IndexError):
            self.locked = False

    def _maybe_notify(self, state: dict, now: float, daemon_error: str) -> None:
        if self.notifier is None:
            return
        if self.locked:
            # A prompt shown before locking is stale; allow it again on unlock.
            self._close_notifier()
            return
        pending = state.get("pending_notification")
        if not self.ready or daemon_error or self.error_code:
            pending = None
        try:
            self.notifier.sync(pending, allow_retry=True, now=now)
        except (GLib.Error, OSError, ValueError, errors.SessionError) as error:
            log.warning(
                "cannot update the tent notification: %s", describe_error(error)
            )

    def _close_notifier(self) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.close()
        except (GLib.Error, OSError, ValueError) as error:  # pragma: no cover
            log.debug("closing the notification failed: %s", describe_error(error))

    def _restore_display(self) -> None:
        if self.display is None:
            return
        try:
            self.display.restore()
        except (DisplayError, GLib.Error, OSError, ValueError) as error:
            # The journal is kept so a later attempt (or the next start) can retry.
            log.warning("display restore pending: %s", describe_error(error))

    def _reset_display_retry(self) -> None:
        self.retry_display = False
        self.retry_at = 0.0
        self.retry_delay = 0.0

    # -- notification actions -------------------------------------------

    def handle_action(self, parameters: Any) -> None:
        """Confirm the current tent notification when its action is invoked."""
        notifier = self.notifier
        if notifier is None or self.client is None:
            return
        try:
            notification, action = parameters.unpack()
        except (TypeError, ValueError):
            return
        if notification != notifier.notification or action not in NOTIFY_ACTIONS:
            return
        epoch = notifier.epoch
        if epoch is None:
            return
        try:
            self.client.confirm_tent(epoch)
        except (ServiceError, GLib.Error, OSError, ValueError) as error:
            log.warning("tent confirmation rejected: %s", describe_error(error))
        finally:
            self._close_notifier()


__all__ = [
    "AUTH_ERROR_DETAIL_MARKERS",
    "AUTH_ERROR_NAMES",
    "NOTIFY_ACTIONS",
    "SessionRuntime",
    "TRANSIENT_RETRY_SECONDS",
]
