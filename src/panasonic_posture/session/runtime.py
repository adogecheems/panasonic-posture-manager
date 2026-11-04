"""Per-tick session state machine for the unprivileged adapter.

The process shell in :mod:`panasonic_posture.session.agent` owns bus names,
signals and GLib lifecycle.  This module owns everything that happens while the
adapter is attached to the service: mirroring generations, applying display
transactions, reporting the lease heartbeat, retrying transient failures,
checking the screen lock and synchronising the posture notification.

Keeping the runtime separate makes the failure/retry state machine directly
testable and prevents the D-Bus process shell from accumulating display and
notification policy again.
"""

from __future__ import annotations

import logging
from collections import deque
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from typing import Any, Callable

from gi.repository import GLib

from .. import protocol
from ..model import StateSnapshot
from ..tasks import thread_executor
from . import errors
from .errors import DisplayError, ErrorCode, ServiceError, describe_error
from .lockstate import query_locked

log = logging.getLogger(__name__)

NOTIFY_ACTIONS = frozenset({"default", "tent", "stand"})

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


@dataclass
class _Task:
    """One serialized blocking operation and the state needed to finish it."""

    kind: str
    future: Future[Any]
    context: Any = None


class SessionRuntime:
    """Reconcile one session adapter client with the daemon and the desktop."""

    def __init__(
        self,
        client: Any,
        display: Any,
        notifier: Any,
        *,
        session_bus: Any = None,
        system_bus: Any = None,
        screen_locked: Callable[[], bool] | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.client = client
        self.display = display
        self.notifier = notifier
        self._session_bus = session_bus
        self._system_bus = system_bus
        self._screen_locked = screen_locked
        self._executor = executor if executor is not None else thread_executor(
            "posture-session-runtime"
        )
        self._owns_executor = executor is None
        self._tasks: deque[_Task] = deque()
        self._closing = False

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

        # display retry (transient compositor/backend failures)
        self.apply_now = False
        self.retry_display = False
        self.retry_at = 0.0
        self.retry_delay = 0.0
        # Startup recovery may have left a journal pending. Ownership outlives
        # per-generation readiness and even a failed or superseded apply.
        self._display_needs_restore = True
        self._restore_retry_at = 0.0

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
        """Advance the serialized reconciliation pipeline without blocking."""
        if self._closing:
            return
        self._drain_tasks(now)
        if self._tasks:
            return
        if self._restore_retry_at and now >= self._restore_retry_at:
            self._restore_display_async(now)
        elif self.failure_active:
            if now < self.failure_retry_at:
                return
            self._submit(
                "probe",
                self.client.session_update,
                self._probe_generation,
                False,
                "",
                "",
            )
        elif (
            self._transient_since is not None
            and now < self._transient_retry_at
            and now - self._transient_since
            < protocol.AGENT_FAILURE_GRACE_SECONDS
        ):
            # A transient transport failure is being retried: keep the display
            # as it is until either the call succeeds or the grace expires.
            return
        else:
            self._submit("state", self.client.get_state)
        self._drain_tasks(now)

    def close(self) -> None:
        """Close the notification and restore the display, best effort."""
        self._closing = True
        tasks = tuple(self._tasks)
        self._tasks.clear()
        for task in tasks:
            try:
                task.future.result()
            except Exception:  # noqa: BLE001 - close must always finish
                log.debug("background task failed during shutdown", exc_info=True)
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        self._close_notifier()
        self._restore_display()

    # -- background task plumbing ----------------------------------------

    def _submit(
        self,
        kind: str,
        function: Callable[..., Any],
        /,
        *args: Any,
        context: Any = None,
    ) -> None:
        try:
            future = self._executor.submit(function, *args)
        except Exception as error:  # noqa: BLE001 - turn shutdown races into results
            future = Future()
            future.set_exception(error)
        self._tasks.append(_Task(kind, future, context))

    def _drain_tasks(self, now: float) -> None:
        for _ in range(16):
            if not self._tasks:
                return
            task = self._tasks[0]
            if not task.future.done():
                return
            self._tasks.popleft()
            try:
                result = task.future.result()
            except Exception as error:  # noqa: BLE001 - errors are runtime input
                self._task_failed(task, now, error)
            else:
                self._task_finished(task, now, result)

    def _task_finished(self, task: _Task, now: float, result: Any) -> None:
        if task.kind == "state":
            if not isinstance(result, StateSnapshot):
                self._handle_service_failure(
                    now, ValueError("GetState returned a non-snapshot value")
                )
                return
            self._handle_state(result, now)
        elif task.kind == "probe":
            self._probe_succeeded()
        elif task.kind == "apply":
            state, _generation, _daemon_error = task.context
            if state.target.mode == "unavailable":
                self._display_needs_restore = False
            self._submit("verify", self.client.get_state, context=task.context)
        elif task.kind == "verify":
            state, generation, _daemon_error = task.context
            if not isinstance(result, StateSnapshot):
                self._handle_service_failure(
                    now, ValueError("GetState returned a non-snapshot value")
                )
                return
            if result.instance != self.instance or result.generation != generation:
                # The target changed while the display transaction was running.
                # Never acknowledge the stale generation; reconcile the latest
                # state instead so the newer target wins.
                self._handle_state(result, now)
                return
            self.ready = True
            self.error_code = ""
            self.error_detail = ""
            self.apply_now = False
            self._reset_display_retry()
            self._handle_state(result, now)
        elif task.kind == "feedback":
            feedback, state, daemon_error = task.context
            self.last_feedback = feedback
            self.last_feedback_at = now
            self._reported_error = None
            self._reset_transient()
            self._after_feedback(state, now, daemon_error)
        elif task.kind == "restore":
            self._display_needs_restore = False
            self._restore_retry_at = 0.0
        else:  # pragma: no cover - internal invariant
            log.error("unknown session runtime task %r", task.kind)

    def _task_failed(self, task: _Task, now: float, error: Exception) -> None:
        if task.kind in ("state", "verify", "feedback"):
            self._handle_service_failure(now, error)
        elif task.kind == "probe":
            self._enter_failure(now, error)
        elif task.kind == "apply":
            state, generation, daemon_error = task.context
            if isinstance(error, DisplayError):
                self._fail_display(error.code, error.detail, now)
            else:
                self._fail_display(ErrorCode.INTERNAL, describe_error(error), now)
            self._queue_feedback(state, generation, now, daemon_error)
        elif task.kind == "restore":
            self._restore_retry_at = now + protocol.DISPLAY_RETRY_BASE_SECONDS
            log.warning("display restore pending: %s", describe_error(error))

    # -- lease recovery --------------------------------------------------

    def _probe_succeeded(self) -> None:
        """Finish lease recovery and fetch a fresh state document."""
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
        self._submit("state", self.client.get_state)

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
            self._restore_display_async(now)
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

    def _handle_state(self, state: StateSnapshot, now: float) -> None:
        instance = state.instance
        if instance != self.instance:
            # The daemon restarted: policy epochs and generations start over.
            self.instance = instance
            self.generation = None
            self.error_code = ""
            self.error_detail = ""
            self.last_feedback = None
            self._reset_display_retry()
            self._close_notifier()
        generation = state.generation
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
            self._restore_display_async(now)
            self.ready = False
            self.apply_now = False
            self.retry_display = False
        else:
            if not self.ready and not self.apply_now and not self.retry_display:
                self.apply_now = True  # backend recovered (or first sight)
            if not self._maybe_apply(state, generation, now):
                return

        self._queue_feedback(state, generation, now, daemon_error)

    def _daemon_error(self, state: StateSnapshot) -> str:
        """Return a *backend* error code, ignoring echoes of our own report.

        The state document carries the last failure of the whole pipeline; the
        privileged service may therefore echo the error code we just reported
        through ``SessionUpdate``.  That echo must not disable the local display
        retry loop, so it is filtered out when it matches our own report for the
        current generation.
        """
        code = state.error_code or ""
        if not code:
            return ""
        if code in _ECHO_ERROR_CODES or code == self.error_code:
            # Only the session adapter reports display-* codes, and the
            # current code is an echo of our last report: keep retrying.
            return ""
        return code

    def _maybe_apply(
        self, state: StateSnapshot, generation: int, now: float
    ) -> bool:
        if self.ready:
            return True
        if self._restore_retry_at:
            return True
        if not (self.apply_now or (self.retry_display and now >= self.retry_at)):
            return True
        self._display_needs_restore = True
        self._submit(
            "apply",
            self.display.apply,
            state.target,
            context=(state, generation, self._daemon_error(state)),
        )
        return False

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

    def _queue_feedback(
        self,
        state: StateSnapshot,
        generation: int,
        now: float,
        daemon_error: str,
    ) -> None:
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
            self._after_feedback(state, now, daemon_error)
            return
        self._submit(
            "feedback",
            self.client.session_update,
            *feedback,
            context=(feedback, state, daemon_error),
        )

    def _after_feedback(
        self, state: StateSnapshot, now: float, daemon_error: str
    ) -> None:
        self._maybe_check_lock(now)
        self._maybe_notify(state, now, daemon_error)

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
        self.locked = query_locked(
            system_bus=self._system_bus,
            session_bus=self._session_bus,
        )

    def _maybe_notify(
        self, state: StateSnapshot, now: float, daemon_error: str
    ) -> None:
        if self.notifier is None:
            return
        if self.locked:
            # A prompt shown before locking is stale; allow it again on unlock.
            self._close_notifier()
            return
        pending = state.pending_notification
        prompt = state.pending_prompt
        if prompt not in protocol.PROMPTS:
            # Older daemons only exposed the epoch; their prompt was always
            # the tent prompt.
            prompt = "tent" if pending is not None else None
        if not self.ready or daemon_error or self.error_code:
            pending = None
        try:
            self.notifier.sync(
                pending,
                prompt=prompt or "tent",
                allow_retry=True,
                now=now,
            )
        except (GLib.Error, OSError, ValueError, errors.SessionError) as error:
            log.warning(
                "cannot update the posture notification: %s", describe_error(error)
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

    def _restore_display_async(self, now: float) -> None:
        if (
            self.display is not None
            and self._display_needs_restore
            and now >= self._restore_retry_at
        ):
            self._submit("restore", self.display.restore)

    def _reset_display_retry(self) -> None:
        self.retry_display = False
        self.retry_at = 0.0
        self.retry_delay = 0.0

    # -- notification actions -------------------------------------------

    def handle_action(self, parameters: Any) -> None:
        """Confirm the current posture notification when its action is invoked."""
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
        prompt = getattr(notifier, "prompt", None)
        try:
            if prompt == "stand":
                self.client.confirm_stand(epoch)
            else:
                self.client.confirm_tent(epoch)
        except (ServiceError, GLib.Error, OSError, ValueError) as error:
            log.warning("posture confirmation rejected: %s", describe_error(error))
        finally:
            self._close_notifier()


__all__ = [
    "AUTH_ERROR_DETAIL_MARKERS",
    "AUTH_ERROR_NAMES",
    "NOTIFY_ACTIONS",
    "SessionRuntime",
    "TRANSIENT_RETRY_SECONDS",
]
