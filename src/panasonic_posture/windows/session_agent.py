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

from ..contracts import AGENT_HEARTBEAT_SECONDS, ErrorCode, TICK_SECONDS
from ..session.failures import FailureKind, ServiceError
from ..session.ports import DisplayBackend
from ..session.runtime import SessionRuntime
from ..tasks import thread_executor
from .hardware_release import HardwareReleaseCoordinator
from .session_client import WindowsSessionClient
from .session_context import WindowsSessionContext
from .session_display import (
    QualifiedDisplay as _QualifiedDisplay, SessionContext, SessionHardwareQuery,
    SlatePosture,
)
from .session_qualification import (
    ClosableSessionClient as ClosableClient, QualifiedSessionClient,
    ReleaseAwareSessionClient,
)

log = logging.getLogger(__name__)

AUTHORITY_PROOF_ATTEMPTS = 12
AUTHORITY_PROOF_RETRY_SECONDS = .25


def build_optional_slate(client, journal: Path) -> SlatePosture | None:
    """Open CSM output when available without making display control depend on it."""
    from .convertible_slate import NativeSlateDevice, SlateNotifier
    from .slate_journal import SlateJournal, lease_path

    try:
        return SlateNotifier(
            NativeSlateDevice(),
            SlateJournal(lease_path(journal)),
            hardware_query=SessionHardwareQuery(client.get_state),
        )
    except (OSError, ValueError) as error:
        # CSM configuration belongs to the MSI install. If the device is still
        # unavailable, keep direction control usable and never attempt a write
        # against an unverified indicator.
        log.warning('convertible-slate output unavailable; continuing without CSM: %s', error)
        return None


def build_display(client, journal: Path) -> DisplayBackend:
    """Create display recovery behind the same physical-state gate as apply."""
    from ..session.journal import Journal
    from .display import WindowsDisplay

    return WindowsDisplay(
        journal=Journal(journal),
        hardware_query=SessionHardwareQuery(client.get_state),
    )


def _native_context() -> WindowsSessionContext:
    from .security import WindowsSecurity
    WindowsSecurity().require_unprivileged()
    return WindowsSessionContext()


class WindowsSessionAgent:
    def __init__(self, *, display_factory: Callable[[ClosableClient], DisplayBackend],
                 slate_factory: Callable[[ClosableClient], SlatePosture] | None = None,
                 context_factory: Callable[[], SessionContext] = _native_context,
                 client_factory: Callable[
                     [Callable[[], bool], Callable[[], bool]], ReleaseAwareSessionClient
                 ] = (
                     lambda available, unattended: WindowsSessionClient(
                         unattended=unattended,
                         unavailable=lambda: not available(),
                     )),
                 executor: Executor | None = None, lease_executor: Executor | None = None) -> None:
        self.display_factory, self.slate_factory = display_factory, slate_factory
        self.context_factory, self.client_factory = context_factory, client_factory
        self.executor, self.lease_executor = executor, lease_executor
        self.runtime: SessionRuntime | None = None
        self.context: SessionContext | None = None
        self.client: ReleaseAwareSessionClient | None = None
        self._stack = ExitStack()
        self._started = self._closed = False
        self._qualification_task: Future | None = None
        self._heartbeat_at = float('-inf')
        self.hardware_release: HardwareReleaseCoordinator | None = None

    def _prove_receipt_authority(self) -> None:
        state = self.client.get_state()
        for attempt in range(AUTHORITY_PROOF_ATTEMPTS):
            self.client.session_update(state.generation, False)
            current = self.client.get_state()
            if current.instance != state.instance:
                raise ServiceError(
                    ErrorCode.INTERNAL,
                    'session authority changed during startup',
                    kind=FailureKind.REJECTED,
                )
            if current.generation == state.generation and current.session_connected:
                return
            state = current
            if attempt + 1 < AUTHORITY_PROOF_ATTEMPTS:
                # Policy startup can publish laptop, unavailable and the final
                # stable pose in quick succession. Retry against the current
                # generation instead of forcing the supervisor to replace an
                # otherwise authenticated session process.
                time.sleep(AUTHORITY_PROOF_RETRY_SECONDS)
        raise ServiceError(
            ErrorCode.INTERNAL,
            'session authority changed during startup',
            kind=FailureKind.REJECTED,
        )

    def _start_runtime(self) -> None:
        # Display recovery queries the same physical gate as normal apply.
        # Run it on the receipt worker so startup occupies the same RPC
        # channel instead of creating a third channel that the lease worker
        # cannot use later.
        self._prove_receipt_authority()
        self._create_runtime()

    def start(self) -> None:
        if self._started or self._closed:
            raise RuntimeError('session agent already started or closed')
        self._started = True
        try:
            self._create_process_boundary()
            # Prove authority on the exact display worker that will later send
            # receipts, rather than consuming an extra RPC channel on the main
            # thread.
            self.executor.submit(self._start_runtime).result(timeout=20)
        except BaseException:
            self.close()
            raise

    def _create_process_boundary(self) -> None:
        self.context = self.context_factory()
        self._stack.callback(self.context.close)
        self.client = self.client_factory(
            self.context.control_available,
            self.context.unattended,
        )
        self._stack.callback(self.client.close)
        if self.executor is None:
            self.executor = thread_executor('windows-session-display')
            self._stack.callback(self.executor.shutdown, wait=True, cancel_futures=True)
        if self.lease_executor is None:
            self.lease_executor = thread_executor('windows-session-lease')
            self._stack.callback(self.lease_executor.shutdown, wait=True, cancel_futures=True)

    def _create_runtime(self) -> None:
        qualified_client = QualifiedSessionClient(self.client, self.context)
        display = self.display_factory(qualified_client)
        try:
            slate = (self.slate_factory(qualified_client)
                     if self.slate_factory is not None else None)
        except BaseException:
            display.restore()
            raise
        if slate is not None:
            def close_slate() -> None:
                try:
                    closing = self.executor.submit(slate.close)
                except Exception:
                    slate.close()
                else:
                    closing.result()

            self._stack.callback(close_slate)
        qualified = _QualifiedDisplay(display, self.context, slate)

        def close_runtime() -> None:
            if self.runtime is not None:
                self.runtime.close()
            else:
                qualified.restore()

        # LIFO close first drains runtime workers, which performs the final
        # display restore and CSM release on the display worker. A failed
        # runtime construction still restores through the qualified display.
        self._stack.callback(close_runtime)
        self.hardware_release = HardwareReleaseCoordinator(qualified)
        self.runtime = SessionRuntime(
            qualified_client,
            qualified,
            None,
            screen_locked=lambda: not self.context.control_available(),
            executor=self.executor,
            lease_executor=self.lease_executor,
            defer_hardware_handoff=True,
            hardware_release_target=self.hardware_release.target_for,
            lease_keepalive=True,
            finalize_display_on_executor=True,
        )

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
    from .diagnostics import configure_logging
    from .instance import InstanceLock
    from .session_stop import NativeStopSignal

    configure_logging(options.journal)

    def display_factory(client):
        return build_display(client, options.journal)

    def slate_factory(client):
        return build_optional_slate(client, options.journal)

    agent = WindowsSessionAgent(display_factory=display_factory, slate_factory=slate_factory)
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
        log.exception('native session unavailable')
        print(f'native session unavailable: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
