"""Nonblocking authenticated server loop for the hardware named pipe."""
from collections.abc import Callable
from concurrent.futures import Executor, Future
from dataclasses import dataclass
import time

from ..service.engine import Engine, ServiceError
from ..tasks import thread_executor
from .console_identity import ConsoleIdentity
from .hardware_host import HardwareHost
from .hardware_pipe import MAX_CLIENTS, NativePipes, PIPE_NAME, PipeState
from .hardware_protocol import CHANNEL
from .protocol import ProtocolError
from .security import AuthenticationError, WindowsSecurity

AUTH_SECONDS = 1.0
CLIENT_TIMEOUT = 5.0


class ConsoleGuard:
    """Fresh cached decisions; slow session validation cannot renew a lease."""

    def __init__(self, check: Callable[[ConsoleIdentity], bool],
                 agent: ConsoleIdentity | None, *,
                 clock: Callable[[], float] = time.monotonic,
                 executor: Executor | None = None) -> None:
        self.check, self.agent, self.clock = check, agent, clock
        self.executor = executor or thread_executor('windows-session-auth')
        self.owned_executor = executor is None
        self.entries: dict[ConsoleIdentity, tuple[float, bool]] = {}
        self.pending: Future[tuple[float, bool]] | None = None
        self._epoch = 0
        self._pending_epoch = -1
        self._pending_agent: ConsoleIdentity | None = None
        self.next_check = 0.

    def set_agent(self, peer: ConsoleIdentity | None) -> None:
        if peer is not None and not isinstance(peer, ConsoleIdentity):
            raise TypeError('a native process identity is required')
        if peer == self.agent:
            return
        self.agent = peer
        self._epoch += 1
        self.entries.clear()
        self.next_check = 0.
        # Keep a running old check in its one slot. Its result is discarded,
        # rather than creating more workers while WTS is still occupied.

    def remember(self, peer: ConsoleIdentity, checked_at: float, valid: bool = True) -> None:
        self.entries[peer] = checked_at, valid

    def allow(self, peer: ConsoleIdentity) -> bool:
        checked, valid = self.entries.get(peer, (float('-inf'), False))
        return valid is True and 0 <= self.clock() - checked < AUTH_SECONDS

    def forget(self, peer: ConsoleIdentity) -> None:
        if peer != self.agent:
            self.entries.pop(peer, None)

    def tick(self) -> None:
        if self.pending is not None and self.pending.done():
            current = self._pending_epoch == self._epoch and self._pending_agent == self.agent
            try:
                checked, valid = self.pending.result()
                if current and self._pending_agent is not None:
                    self.remember(self._pending_agent, checked, valid)
            except OSError:
                if current and self._pending_agent is not None:
                    self.entries.pop(self._pending_agent, None)
            self.pending = None
        if self.agent is not None and self.pending is None and self.clock() >= self.next_check:
            agent = self.agent

            def check():
                checked = self.clock()
                return checked, self.check(agent)

            self._pending_agent, self._pending_epoch = agent, self._epoch
            self.pending = self.executor.submit(check)
            self.next_check = self.clock() + .5

    def close(self) -> None:
        self.entries.clear()
        if self.owned_executor:
            self.executor.shutdown(wait=False, cancel_futures=True)


@dataclass
class Slot:
    handle: int
    connected: bool = False
    peer: ConsoleIdentity | None = None
    serial: int = 0
    touched: float = 0.
    pending: Future[tuple[ConsoleIdentity, float]] | None = None
    request: dict | None = None
    reply: dict | None = None
    retired: bool = False


class HardwareServer:
    def __init__(self, engine: Engine, *, session_process: ConsoleIdentity | None = None,
                 security: WindowsSecurity | None = None, pipes: NativePipes | None = None,
                 pipe_name: str = PIPE_NAME, clock: Callable[[], float] = time.monotonic,
                 executor: Executor | None = None, guard_executor: Executor | None = None,
                 session_admission: Callable[[], bool] | None = None) -> None:
        self.security = security or WindowsSecurity()
        self.security.require_system()
        self.pipes = pipes or NativePipes(self.security)
        self.clock = clock
        self.guard = ConsoleGuard(
            self.security.is_active, session_process, clock=clock, executor=guard_executor,
        )
        self.host = HardwareHost(
            engine, authorize=self.guard.allow, session_process=session_process,
            session_admission=session_admission,
        )
        self.executor = executor or thread_executor('windows-request-auth')
        self.owned_executor = executor is None
        self.slots: list[Slot] = []
        self.closed = False
        try:
            for index in range(MAX_CLIENTS):
                self.slots.append(Slot(self.pipes.create(pipe_name, first=index == 0)))
        except BaseException:
            self.close()
            raise

    def register_session_process(self, peer: ConsoleIdentity | None) -> None:
        """Host-only lifecycle operation, never a method on the RPC channel."""
        if self.closed:
            raise OSError('hardware server is closed')
        if peer is not None and not isinstance(peer, ConsoleIdentity):
            raise TypeError('a native process identity is required')
        if peer == self.host.session_process:
            return
        old = self.host.session_process
        if old is not None:
            self.host.disconnected(old)
        self.host.session_process = peer
        self.guard.set_agent(peer)
        # An authentication worker may still be holding a previous process's
        # pipe. Retire it before registering the replacement; no old request
        # may run after a trusted process switch.
        for slot in self.slots:
            self._reset(slot)

    def _reset(self, slot: Slot) -> None:
        if slot.peer is not None:
            self.host.disconnected(slot.peer)
            self.guard.forget(slot.peer)
        slot.peer = None
        # Keep the handle while an authentication worker uses it. At most four
        # slots/jobs exist, including timed-out requests, even if WTS hangs.
        if slot.pending is not None and not slot.pending.done():
            slot.retired = True
            return
        self.pipes.reset(slot.handle)
        slot.connected, slot.serial, slot.pending = False, 0, None
        slot.request, slot.reply, slot.retired = None, None, False

    def poll(self) -> None:
        if self.closed:
            return
        self.guard.tick()
        self.host.tick()
        for slot in self.slots:
            try:
                self._poll_slot(slot)
            except (OSError, ValueError):
                self._reset(slot)

    def _poll_slot(self, slot: Slot) -> None:
        if slot.retired:
            if slot.pending is None or slot.pending.done():
                self._reset(slot)
            return
        if not self._connect(slot):
            return
        if self.clock() - slot.touched >= CLIENT_TIMEOUT:
            self._reset(slot)
            return
        if slot.pending is not None and not self._finish_authentication(slot):
            return
        if slot.reply is not None:
            result = self.pipes.write(slot.handle, slot.reply)
            if result.state is PipeState.PENDING:
                return
            if result.state is not PipeState.READY:
                raise ProtocolError('hardware pipe rejected a response')
            slot.request = slot.reply = None
            slot.touched = self.clock()
            return
        self._read_request(slot)

    def _connect(self, slot: Slot) -> bool:
        if slot.connected:
            return True
        result = self.pipes.accept(slot.handle)
        if result.state is PipeState.PENDING:
            return False
        if result.state is not PipeState.READY or result.value is not True:
            return False
        slot.connected, slot.touched = True, self.clock()
        return True

    def _finish_authentication(self, slot: Slot) -> bool:
        pending = slot.pending
        if pending is None:
            raise RuntimeError('authentication state is missing')
        if not pending.done():
            return False
        peer, checked = pending.result()
        slot.pending = None
        if slot.peer is not None and slot.peer != peer:
            raise AuthenticationError('pipe client identity changed')
        slot.peer = peer
        self.guard.remember(peer, checked)
        request = slot.request
        if (not isinstance(request, dict) or type(request.get('id')) is not int or
                request['id'] <= slot.serial):
            raise ProtocolError('replayed hardware request')
        slot.serial = request['id']
        try:
            reply = self.host.dispatch(peer, request)
            reply['error'] = None
        except ServiceError as error:
            reply = dict(
                version=1, channel=CHANNEL, type='response', id=request['id'],
                instance=self.host.instance, accepted=False, error=error.code,
                error_detail=error.detail,
                state=self.host.engine.state.to_dict(),
                hardware_release_pose=self.host.engine.hardware_release_pose,
            )
        slot.reply = reply
        return True

    def _read_request(self, slot: Slot) -> None:
        result = self.pipes.read(slot.handle)
        if result.state is not PipeState.READY:
            return
        request = result.value
        if not isinstance(request, dict):
            raise ProtocolError('invalid hardware request')
        slot.request = request

        def authenticate(handle=slot.handle):
            checked = self.clock()
            return self.security.peer(handle), checked

        slot.pending = self.executor.submit(authenticate)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.host.close()
        finally:
            handles = []
            for slot in self.slots:
                if slot.pending is not None and not slot.pending.done():
                    # peer() owns the handle until it returns. Close it from
                    # the worker's completion callback instead of racing the
                    # in-flight WTS lookup with an unlink/close operation.
                    slot.pending.add_done_callback(
                        lambda _future, handle=slot.handle: self.pipes.close(handle)
                    )
                else:
                    handles.append(slot.handle)
            self.slots.clear()
            for handle in handles:
                self.pipes.close(handle)
            self.guard.close()
            if self.owned_executor:
                self.executor.shutdown(wait=False, cancel_futures=True)


__all__ = [
    'AUTH_SECONDS', 'CLIENT_TIMEOUT', 'ConsoleGuard', 'HardwareServer', 'Slot',
]
