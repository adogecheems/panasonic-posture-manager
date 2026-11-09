"""Owner-loop coordination of one trusted session child and one launch worker.

The caller keeps polling the hardware server throughout start and stop.
Registration/revocation is owner-loop work; WTS, process creation, resume and
cleanup use the single bounded worker. A normal stop signals the child first;
forced termination is reserved for failure or an expired restoration grace.
"""
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
import threading
import time

from ..tasks import thread_executor
from .hardware_host import ConsoleIdentity
from .session_launch import ConsoleUser, ManagedSession, NativeSessionLauncher

PROBE_SECONDS = .5
STOP_GRACE_SECONDS = 5.
RESULT_MAX_AGE = 5.


@dataclass
class _Task:
    kind: str
    future: Future
    started: float
    user: ConsoleUser | None = None


class SessionSupervisor:
    def __init__(self, launcher: NativeSessionLauncher,
                 register: Callable[[ConsoleIdentity | None], None], *,
                 clock: Callable[[], float] = time.monotonic, executor: Executor | None = None) -> None:
        self.launcher, self.register, self.clock = launcher, register, clock
        self.executor = executor if executor is not None else thread_executor('windows-session-launch')
        self._owned_executor = executor is None
        self._owner = threading.get_ident()
        self.child: ManagedSession | None = None
        self.user: ConsoleUser | None = None
        self.error: str | None = None
        self.closed = self.stopping = self._retiring = False
        self._stop_at = self._next = 0.
        self._task: _Task | None = None

    def _owner_only(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError('session supervisor requires its owner loop')

    def _submit(self, kind: str, function: Callable, user: ConsoleUser | None = None) -> None:
        if self._task is not None:
            raise RuntimeError('only one native lifecycle operation may be pending')
        started = self.clock()
        try:
            future = self.executor.submit(function)
        except RuntimeError as error:
            future = Future()
            future.set_exception(error)
        self._task = _Task(kind, future, started, user)

    def _retire(self) -> None:
        self.register(None)
        if self.child is not None and not self._retiring:
            self._retiring = True
            self._stop_at = self.clock() + STOP_GRACE_SECONDS
            self.child.request_stop()  # SetEvent: immediate, no WTS or wait.

    def request_stop(self) -> None:
        self._owner_only()
        if self.closed or self.stopping:
            return
        self.stopping = True
        self._retire()

    def _completed(self, task: _Task) -> None:
        if task.kind == 'create':
            # Retain even a late or stop-time child until it has been closed.
            self.child = task.future.result()
            self.user = task.user
            if (self.stopping or not 0 <= self.clock() - task.started <= RESULT_MAX_AGE):
                self._retire()
                return
            self.register(self.child.identity)
            self._submit('resume', self.child.resume)
        elif task.kind == 'resume':
            task.future.result()
            self.error = None
            self._next = self.clock() + PROBE_SECONDS
        elif task.kind == 'probe':
            user = task.future.result()
            if self.stopping:
                return
            if not 0 <= self.clock() - task.started <= RESULT_MAX_AGE:
                raise OSError('console observation expired before dispatch')
            if self.child is not None:
                if user is None or self.user is None or user.key != self.user.key or not self.child.alive():
                    self._retire()
            elif user is not None and user.unlocked:
                self._submit('create', lambda: self.launcher.create(user), user)
            self._next = self.clock() + PROBE_SECONDS
        elif task.kind == 'reap':
            task.future.result()
            self.child = self.user = None
            self._retiring = False
            self._next = self.clock() + PROBE_SECONDS
        elif task.kind == 'shutdown':
            task.future.result()
            self.closed = True
            if self._owned_executor:
                self.executor.shutdown(wait=False)

    def poll(self) -> None:
        self._owner_only()
        if self.closed:
            return
        if self._task is not None:
            if not self._task.future.done():
                return
            task, self._task = self._task, None
            try:
                self._completed(task)
            except (OSError, ValueError, RuntimeError) as error:
                self.error = str(error)[:200]
                self._next = self.clock() + PROBE_SECONDS
                if task.kind == 'create' and self.child is None:
                    # A failed create may have retained resources after an
                    # incomplete cleanup. Reap them before trying another PID.
                    self.child = getattr(self.launcher, 'pending_child', None)
                if self.child is not None:
                    try:
                        self._retire()
                    except OSError as stopping_error:
                        self.error = str(stopping_error)[:200]
            if self._task is not None or self.closed:
                return
        if self._retiring and self.child is not None:
            try:
                exited = not self.child.alive()  # Zero-time process wait.
            except OSError:
                exited = False
                self._stop_at = self.clock()
            if self.clock() >= self._next and (exited or not self.child.resumed or self.clock() >= self._stop_at):
                self._submit('reap', self.child.close)
            return
        if self.stopping:
            if self.child is not None:
                self._retire()
            elif self.clock() >= self._next:
                self._submit('shutdown', self.launcher.close)
            return
        if self.clock() >= self._next:
            self._submit('probe', self.launcher.probe)

    def close(self) -> None:
        """Finalize only after request_stop/poll has finished child cleanup."""
        self._owner_only()
        if not self.closed:
            self.request_stop()
            raise OSError('continue polling until session shutdown completes')
