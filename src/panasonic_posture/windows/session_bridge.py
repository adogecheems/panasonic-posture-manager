"""Thread-safe in-process session receipts for the shared Windows engine."""
from concurrent.futures import Future, TimeoutError
import queue
import threading
from collections.abc import Callable

from ..contracts import ErrorCode
from ..model import StateSnapshot
from ..service.engine import Engine
from ..service.ports import SessionOwner
from ..session.failures import FailureKind, ServiceError


class SessionBridge:
    """Private capability; it is not an externally callable receipt API."""

    def __init__(self, state: StateSnapshot, available: Callable[[], bool],
                 unattended: Callable[[], bool] = lambda: False) -> None:
        self._lock = threading.Lock()
        self._state, self.available = state, available
        self.unattended = unattended
        self._requests: queue.Queue[
            tuple[Future[None], tuple[int, bool, str | None, str | None]]
        ] = queue.Queue(maxsize=4)
        self._accepted: list[Future[None]] = []
        self._closed = False

    @staticmethod
    def rejected() -> ServiceError:
        return ServiceError(
            ErrorCode.DISPLAY_UNAVAILABLE,
            'screen-session-unavailable',
            kind=FailureKind.REJECTED,
        )

    def publish(self, state: StateSnapshot) -> None:
        with self._lock:
            self._state = state
            accepted, self._accepted = self._accepted, []
        # SessionRuntime probes by reading immediately after a receipt. The
        # response must not race ahead of the owner tick's accepted snapshot.
        for future in accepted:
            if not future.done():
                future.set_result(None)

    def get_state(self) -> StateSnapshot:
        with self._lock:
            if self._closed or not self.available():
                raise self.rejected()
            return self._state

    def session_update(self, generation: int, ready: bool, error_code: str | None = None,
                       error_detail: str | None = None) -> None:
        future: Future[None] = Future()
        with self._lock:
            if self._closed or not self.available():
                raise self.rejected()
            try:
                self._requests.put_nowait(
                    (future, (generation, ready, error_code, error_detail))
                )
            except queue.Full as error:
                raise self.rejected() from error
        try:
            future.result(timeout=1.)
        except TimeoutError as error:
            # Cancellation prevents an old queued receipt renewing a lease.
            future.cancel()
            raise self.rejected() from error

    def drain(self, engine: Engine, owner: SessionOwner) -> None:
        for _ in range(4):
            try:
                future, args = self._requests.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if (self._closed or not self.available() or not engine.session_update(
                        *args, owner=owner, unattended=self.unattended())):
                    raise self.rejected()
            except Exception as error:
                future.set_exception(error)
            else:
                self._accepted.append(future)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            accepted, self._accepted = self._accepted, []
            for future in accepted:
                if not future.done():
                    future.set_exception(self.rejected())
            while True:
                try:
                    future, _ = self._requests.get_nowait()
                except queue.Empty:
                    break
                if not future.done():
                    future.set_exception(self.rejected())


__all__ = ['SessionBridge']
