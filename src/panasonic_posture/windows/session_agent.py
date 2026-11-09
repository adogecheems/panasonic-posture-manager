"""Explicit nonprivileged native session process, never started by the UI.

The installed host must register this exact process before allowing receipts.
Startup proves that authority on the same worker used by SessionRuntime,
before constructing a display backend (whose journal may require recovery).
"""
import argparse
from collections.abc import Callable
from concurrent.futures import Executor, Future
from contextlib import ExitStack
import logging
from pathlib import Path
import sys
import time
from typing import Protocol

from ..contracts import AGENT_HEARTBEAT_SECONDS, ErrorCode, TICK_SECONDS
from ..model import StateSnapshot
from ..session.failures import FailureKind, ServiceError
from ..session.ports import DisplayBackend, SessionClient
from ..session.runtime import SessionRuntime
from ..tasks import thread_executor
from .session_client import WindowsSessionClient
from .session_context import WindowsSessionContext
from .session_display import SessionContext, QualifiedDisplay as _QualifiedDisplay

log = logging.getLogger(__name__)


class ClosableClient(SessionClient, Protocol):
    def close(self) -> None: ...


class _QualifiedClient:
    def __init__(self, client: ClosableClient, context: SessionContext) -> None:
        self.client, self.context = client, context

    @staticmethod
    def _rejected() -> ServiceError:
        return ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, 'native session control is unavailable', kind=FailureKind.REJECTED)

    def get_state(self) -> StateSnapshot:
        state = self.client.get_state()
        if not self.context.control_available():
            self.client.session_update(state.generation, False, ErrorCode.DISPLAY_UNAVAILABLE, 'native-session-unavailable')
            raise self._rejected()
        return state

    def session_update(self, generation: int, ready: bool, error_code: str | None = None,
                       error_detail: str | None = None) -> None:
        if not self.context.control_available():
            self.client.session_update(generation, False, ErrorCode.DISPLAY_UNAVAILABLE, 'native-session-unavailable')
            raise self._rejected()
        self.client.session_update(generation, ready, error_code, error_detail)
        if not self.context.control_available():
            raise self._rejected()

    def confirm_tent(self, epoch: int) -> None:
        if not self.context.control_available():
            raise self._rejected()
        self.client.confirm_tent(epoch)

    def confirm_stand(self, epoch: int) -> None:
        if not self.context.control_available():
            raise self._rejected()
        self.client.confirm_stand(epoch)

    def wake_screen(self, generation: int, serial: int) -> None:
        if not self.context.control_available():
            raise self._rejected()
        self.client.wake_screen(generation, serial)


def _native_context() -> WindowsSessionContext:
    from .security import WindowsSecurity
    WindowsSecurity().require_unprivileged()
    return WindowsSessionContext()


class WindowsSessionAgent:
    def __init__(self, *, display_factory: Callable[[], DisplayBackend],
                 context_factory: Callable[[], SessionContext] = _native_context,
                 client_factory: Callable[[Callable[[], bool]], ClosableClient] = (
                     lambda available: WindowsSessionClient(unavailable=lambda: not available())),
                 executor: Executor | None = None, lease_executor: Executor | None = None) -> None:
        self.display_factory, self.context_factory, self.client_factory = display_factory, context_factory, client_factory
        self.executor, self.lease_executor = executor, lease_executor
        self.runtime: SessionRuntime | None = None
        self.context: SessionContext | None = None
        self.client: ClosableClient | None = None
        self._stack = ExitStack()
        self._started = self._closed = False
        self._qualification_task: Future | None = None
        self._heartbeat_at = float('-inf')

    def _prove_receipt_authority(self) -> None:
        state = self.client.get_state()
        self.client.session_update(state.generation, False)
        current = self.client.get_state()
        if (current.instance != state.instance or current.generation != state.generation or
                not current.session_connected):
            raise ServiceError(ErrorCode.INTERNAL, 'session authority changed during startup', kind=FailureKind.REJECTED)

    def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError('session agent already started or closed')
        self._started = True
        try:
            self.context = self.context_factory()
            self._stack.callback(self.context.close)
            self.client = self.client_factory(self.context.control_available)
            self._stack.callback(self.client.close)
            if self.executor is None:
                self.executor = thread_executor('windows-session-display')
                self._stack.callback(self.executor.shutdown, wait=True, cancel_futures=True)
            if self.lease_executor is None:
                self.lease_executor = thread_executor('windows-session-lease')
                self._stack.callback(self.lease_executor.shutdown, wait=True, cancel_futures=True)
            # Reuse this worker, rather than consuming a third RPC channel.
            self.executor.submit(self._prove_receipt_authority).result(timeout=20)
            display = self.display_factory()
            self._stack.callback(lambda: self.runtime.close() if self.runtime else display.restore())
            self.runtime = SessionRuntime(_QualifiedClient(self.client, self.context),
                _QualifiedDisplay(display, self.context), None,
                screen_locked=lambda: not self.context.control_available(),
                executor=self.executor, lease_executor=self.lease_executor)
        except BaseException:
            self.close()
            raise

    def _qualification_heartbeat(self) -> None:
        state = self.client.get_state()
        self.client.session_update(state.generation, False, ErrorCode.DISPLAY_UNAVAILABLE, 'native-session-unavailable')

    def tick(self, now: float) -> None:
        if not self._started or self._closed:
            return
        self.context.poll()
        unavailable = not self.context.control_available()
        if self._qualification_task is not None:
            if self._qualification_task.done():
                try:
                    self._qualification_task.result()
                except (OSError, ValueError, ServiceError) as error:
                    log.debug('unavailable-session heartbeat rejected: %s', error)
                self._qualification_task = None
            elif not unavailable:
                # A late not-ready renewal must complete before any new ready
                # report. Otherwise it could revoke a resumed transaction.
                return
        if unavailable and self._qualification_task is None and now >= self._heartbeat_at:
            self._heartbeat_at = now + AGENT_HEARTBEAT_SECONDS
            self._qualification_task = self.lease_executor.submit(self._qualification_heartbeat)
        self.runtime.tick(now)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._stack.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--journal', type=Path, required=True, help='explicit per-user display recovery journal')
    parser.add_argument('--stop-event', help='SYSTEM-owned stop signal from the trusted launcher')
    options = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('native session agent requires Windows')
    from ..session.journal import Journal
    from .display import WindowsDisplay
    from .instance import InstanceLock
    from .session_stop import NativeStopSignal
    agent = WindowsSessionAgent(display_factory=lambda: WindowsDisplay(journal=Journal(options.journal)))
    try:
        with ExitStack() as stack:
            stop = NativeStopSignal.open(options.stop_event) if options.stop_event is not None else None
            if stop is not None:
                stack.callback(stop.close)
                if stop.is_set():
                    return 0
            stack.enter_context(InstanceLock(options.journal))
            try:
                agent.start()
                while stop is None or not stop.is_set():
                    agent.tick(time.monotonic())
                    time.sleep(TICK_SECONDS)
            finally:
                agent.close()
        return 0
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, ServiceError) as error:
        print(f'native session unavailable: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
