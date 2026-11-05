"""Session state machine for the unprivileged adapter.

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

from .. import contracts
from ..model import StateSnapshot
from ..tasks import thread_executor
from . import failures as errors
from .failures import DisplayError, ErrorCode, FailureKind, ServiceError, describe_error
from .ports import DisplayBackend, PostureNotifier, SessionClient

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

#: Delay before retrying after a transient transport failure (the hard grace
#: before the display is restored is ``contracts.AGENT_FAILURE_GRACE_SECONDS``).
TRANSIENT_RETRY_SECONDS = 0.5
HARDWARE_SETTLE_SECONDS = 2.0


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
        client: SessionClient,
        display: DisplayBackend,
        notifier: PostureNotifier | None,
        *,
        screen_locked: Callable[[], bool] | None = None,
        adapter_errors: tuple[type[Exception], ...] = (),
        executor: Executor | None = None,
        lease_executor: Executor | None = None,
        schedule_completion: Callable[[], None] | None = None,
    ) -> None:
        self.client = client
        self.display = display
        self.notifier = notifier
        self._screen_locked = screen_locked
        self._adapter_errors = (OSError, ValueError, *adapter_errors)
        self._executor = executor if executor is not None else thread_executor(
            "posture-session-runtime"
        )
        self._owns_executor = executor is None
        self._lease_executor = lease_executor if lease_executor is not None else thread_executor(
            "posture-session-lease"
        )
        self._owns_lease_executor = lease_executor is None
        self._lease_task: Future[Any] | None = None
        self._lease_wait_since: float | None = None
        self._lease_attempt_at = float("-inf")
        self._schedule_completion = schedule_completion
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
        self._hold_key: tuple[str, int] | None = None
        self._hold_until = 0.0

        # lease/authorization failure mode
        self.failure_active = False
        self._failure_retry_delay = 0.0
        self.failure_retry_at = 0.0
        self._reported_error: str | None = None

        # transient transport failure grace (a single NoReply must not restore)
        self._transient_since: float | None = None
        self._transient_retry_at = 0.0

    # -- public tick -----------------------------------------------------

    def tick(self, now: float) -> None:
        """Advance the serialized reconciliation pipeline without blocking."""
        if self._closing:
            return
        self.drain_completed_tasks(now)
        if self._tasks:
            self._maintain_display_lease(now)
            return
        self._lease_wait_since = None
        if self._restore_retry_at and now >= self._restore_retry_at:
            self._restore_display_async(now)
        elif self.failure_active:
            if now < self.failure_retry_at:
                return
            self._submit("probe", self._probe_lease)
        elif (
            self._transient_since is not None
            and now < self._transient_retry_at
            and now - self._transient_since
            < contracts.AGENT_FAILURE_GRACE_SECONDS
        ):
            # A transient transport failure is being retried: keep the display
            # as it is until either the call succeeds or the grace expires.
            return
        else:
            self._submit("state", self.client.get_state)
        self.drain_completed_tasks(now)

    def close(self) -> None:
        """Close the notification and restore the display, best effort."""
        self._closing = True
        tasks = tuple(self._tasks)
        self._tasks.clear()
        if self._lease_task is not None:
            tasks += (_Task("lease", self._lease_task),)
            self._lease_task = None
        for task in tasks:
            try:
                task.future.result()
            except Exception:  # noqa: BLE001 - close must always finish
                log.debug("background task failed during shutdown", exc_info=True)
        if self._owns_executor:
            self._executor.shutdown(wait=True, cancel_futures=True)
        if self._owns_lease_executor:
            self._lease_executor.shutdown(wait=True, cancel_futures=True)
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
        schedule = self._schedule_completion
        if schedule is not None and not future.done():
            # This callback may run on the worker. Only the main loop drains
            # results and advances the transaction.
            future.add_done_callback(lambda _future: schedule())

    def drain_completed_tasks(self, now: float) -> None:
        """Advance pending work without starting another periodic state read."""
        if self._closing:
            return
        self._poll_display_lease(now)
        for _ in range(16):
            if not self._tasks:
                return
            task = self._tasks[0]
            if not task.future.done():
                return
            if task.kind in ("apply", "hold", "restore") and self._lease_task is not None:
                # A not-ready heartbeat must finish before a ready report, or
                # its late arrival would revoke the just-confirmed transaction.
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
            self._probe_succeeded(result, now)
        elif task.kind == "hold":
            if not self.failure_active:
                self._submit("state", self.client.get_state)
        elif task.kind == "apply":
            if self.failure_active:
                return  # finish the queued restore, never verify rejected work
            state, _generation, _daemon_error = task.context
            if state.target.mode == "unavailable":
                self._display_needs_restore = False
            self._submit("verify", self.client.get_state, context=task.context)
        elif task.kind == "verify":
            if self.failure_active:
                return
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
        elif task.kind in ("apply", "hold"):
            self._hold_until = 0.0
            state, generation, daemon_error = task.context
            if isinstance(error, DisplayError):
                self._fail_display(error.code, error.detail, now)
            else:
                self._fail_display(ErrorCode.INTERNAL, describe_error(error), now)
            if task.kind == "hold":
                self._restore_display_async(now)
            self._queue_feedback(state, generation, now, daemon_error)
        elif task.kind == "restore":
            self._restore_retry_at = now + contracts.DISPLAY_RETRY_BASE_SECONDS
            log.warning("display restore pending: %s", describe_error(error))

    # -- lease recovery --------------------------------------------------

    def _maintain_display_lease(self, now: float) -> None:
        if self.failure_active or self._lease_task is not None:
            return
        task = self._tasks[0]
        if task.kind not in ("apply", "hold", "restore") or task.future.done():
            self._lease_wait_since = None
            return
        if self._lease_wait_since is None:
            self._lease_wait_since = now
        last = max(self.last_feedback_at, self._lease_attempt_at)
        if last == float("-inf"):
            last = self._lease_wait_since
        if now - last < contracts.AGENT_HEARTBEAT_SECONDS:
            return
        self._lease_attempt_at = now
        # Display work remains serialized on its own worker. This independent
        # lane only renews a verified current lease; it never reports readiness.
        try:
            self._lease_task = self._lease_executor.submit(self._probe_lease)
        except Exception as error:
            self._handle_service_failure(now, error)
            return
        schedule = self._schedule_completion
        if schedule is not None and not self._lease_task.done():
            self._lease_task.add_done_callback(lambda _future: schedule())
        self._poll_display_lease(now)

    def _poll_display_lease(self, now: float) -> None:
        task = self._lease_task
        if task is None or not task.done():
            return
        self._lease_task = None
        try:
            state = task.result()
        except Exception as error:
            self._handle_service_failure(now, error)
            return
        self.last_feedback = (state.generation, False, "", "")
        self.last_feedback_at = now
        self._reset_transient()

    def _probe_lease(self) -> StateSnapshot:
        state = self.client.get_state()
        self.client.session_update(state.generation, False, "", "")
        current = self.client.get_state()
        # The service silently ignores stale reports. A successful reply only
        # proves lease recovery if the report matched the same service and target.
        if (
            current.instance != state.instance
            or current.generation != state.generation
            or not current.session_connected
        ):
            raise ValueError("session lease probe no longer matches service state")
        return current

    def _probe_succeeded(self, state: StateSnapshot, now: float) -> None:
        log.info("session adapter lease restored")
        self._reset_transient()
        self.failure_active = False
        self._failure_retry_delay = 0.0
        self.failure_retry_at = 0.0
        self._handle_state(state, now)

    def _enter_failure(self, now: float, error: BaseException) -> None:
        """Stop display transactions after an authorization/lease failure."""
        self._reset_transient()
        self._hold_until = 0.0
        if not self.failure_active:
            self.failure_active = True
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
            self.generation = None
            self._close_notifier()
            self._restore_display_async(now)
        self._failure_retry_delay = min(
            contracts.DISPLAY_RETRY_MAX_SECONDS,
            self._failure_retry_delay * 2 or contracts.DISPLAY_RETRY_BASE_SECONDS,
        )
        self.failure_retry_at = now + self._failure_retry_delay

    # -- transient vs. rejected failures (P3) ----------------------------

    def _reset_transient(self) -> None:
        self._transient_since = None
        self._transient_retry_at = 0.0

    @staticmethod
    def _is_lease_rejection(error: BaseException) -> bool:
        """Reject immediately only when the client classified the failure."""
        return isinstance(error, ServiceError) and error.kind == FailureKind.REJECTED

    def _handle_service_failure(self, now: float, error: BaseException) -> None:
        if self._is_lease_rejection(error):
            self._reset_transient()
            self._enter_failure(now, error)
            return
        if self._transient_since is None:
            self._transient_since = now
            log.warning(
                "transient session service failure (grace %ss): %s",
                contracts.AGENT_FAILURE_GRACE_SECONDS,
                describe_error(error),
            )
        if now - self._transient_since >= contracts.AGENT_FAILURE_GRACE_SECONDS:
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
            self._hold_until = 0.0
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
        key = (state.instance, generation)
        waiting = (
            state.posture_pending and state.hardware is False
            and state.target.mode == "unavailable" and state.session_connected
            and not state.sensor_error and not state.error_code and state.supported
        )
        if waiting:
            if self._hold_key != key:
                self._hold_key = key
                self._hold_until = now + HARDWARE_SETTLE_SECONDS
                self._display_needs_restore = True
                self._submit("hold", self.display.hold_orientation,
                             context=(state, generation, ""))
                return False
            if now < self._hold_until:
                return True  # heartbeat remains not-ready during the hold
        else:
            self._hold_until = 0.0
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
            self.retry_delay = contracts.DISPLAY_RETRY_BASE_SECONDS
        else:
            self.retry_delay = min(
                contracts.DISPLAY_RETRY_MAX_SECONDS, self.retry_delay * 2
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
            and now - self.last_feedback_at < contracts.AGENT_HEARTBEAT_SECONDS
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
        if now - self._last_lock_check < contracts.SESSION_CHECK_SECONDS:
            return
        self._last_lock_check = now
        if self._screen_locked is None:
            return
        try:
            self.locked = bool(self._screen_locked())
        except self._adapter_errors:
            self.locked = False

    def _maybe_notify(
        self, state: StateSnapshot, now: float, daemon_error: str
    ) -> None:
        if self.notifier is None:
            return
        if self.locked or not self.ready or daemon_error or self.error_code:
            # Allow the prompt again after unlock or session recovery.
            self._close_notifier()
            return
        pending = state.pending_notification
        prompt = state.pending_prompt
        if prompt not in contracts.PROMPTS:
            # Older daemons only exposed the epoch; their prompt was always
            # the tent prompt.
            prompt = "tent" if pending is not None else None
        try:
            self.notifier.sync(
                pending,
                prompt=prompt or "tent",
                allow_retry=True,
                now=now,
            )
        except (*self._adapter_errors, errors.SessionError) as error:
            log.warning(
                "cannot update the posture notification: %s", describe_error(error)
            )

    def _close_notifier(self) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.close()
        except self._adapter_errors as error:  # pragma: no cover
            log.debug("closing the notification failed: %s", describe_error(error))

    def _restore_display(self) -> None:
        if self.display is None:
            return
        try:
            self.display.restore()
        except self._adapter_errors as error:
            # The journal is kept so a later attempt (or the next start) can retry.
            log.warning("display restore pending: %s", describe_error(error))

    def _restore_display_async(self, now: float) -> None:
        if (
            self.display is not None
            and self._display_needs_restore
            and now >= self._restore_retry_at
            and not any(task.kind == "restore" for task in self._tasks)
        ):
            self._submit("restore", self.display.restore)

    def _reset_display_retry(self) -> None:
        self.retry_display = False
        self.retry_at = 0.0
        self.retry_delay = 0.0

    # -- notification actions -------------------------------------------

    def handle_action(self, notification: int, action: str) -> None:
        """Confirm the current posture notification when its action is invoked."""
        notifier = self.notifier
        if notifier is None or self.client is None:
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
        except (*self._adapter_errors, ServiceError) as error:
            log.warning("posture confirmation rejected: %s", describe_error(error))
        finally:
            self._close_notifier()


__all__ = [
    "NOTIFY_ACTIONS",
    "SessionRuntime",
    "TRANSIENT_RETRY_SECONDS",
]
