"""Owner-loop UI client for the independent, authenticated hardware service.

One bounded worker owns its pipe; callbacks and Futures run only in poll().
Disconnect invalidates queued work before completing callbacks. Reconnect
never replays writes or substitutes synthetic state.
"""
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass
import queue
import threading
import time

from panasonic_posture.contracts import ErrorCode, LEASE_SECONDS, MODES
from panasonic_posture.model import StateSnapshot
from panasonic_posture.windows.ipc.protocol import CHANNEL, SIGNATURES, validate_request
from panasonic_posture.windows.ipc.rpc import (
    HardwareConnection,
    HardwareOperationError,
    RPCConnection,
    validated_hardware_state,
)
from panasonic_posture.windows.ipc.owner_loop import OwnerLoop
from panasonic_posture.windows.ipc.frames import MAX_PENDING, ProtocolError
from panasonic_posture.windows.common.ui_operations import UIClientOperations

#: Idle refresh cadence while no user operation is pending.  The service
#: publishes a wake event on every state change and the UI refreshes
#: immediately; this bound only repairs a lost or missed publication, so it
#: stays aligned with the shared service tick instead of the old 0.5 s lag.
IDLE_REFRESH_SECONDS = 0.1


class BackendUnavailable(RuntimeError):
    """The authenticated hardware service is not currently usable."""


@dataclass(frozen=True)
class _Result:
    epoch: int
    serial: int | None
    received: float
    state: StateSnapshot | None = None
    error: Exception | None = None
    lost: bool = False
    accepted: bool | None = None


class HardwareUIClient(UIClientOperations):
    def __init__(self, *, factory: Callable[[], RPCConnection] | None = None, timeout: float = 5.,
                 clock: Callable[[], float] = time.monotonic,
                 resume_control: bool = False) -> None:
        self.factory = factory or (lambda: HardwareConnection(timeout=timeout, clock=clock))
        self.timeout, self.clock = timeout, clock
        self._resume_control = resume_control
        self.instance: str | None = None
        self.pending: dict[int, tuple[Future, float]] = {}
        self._serial = self._epoch = 0
        self._thread: threading.Thread | None = None
        self._active = False
        self._stopped = True
        self._loop = OwnerLoop()
        self._retry_at = 0.
        self._backoff = .5
        self._refresh_future: Future | None = None
        self._state = lambda value: None
        self._unavailable = lambda: None
        self._connected = lambda: None

    def _check_owner(self) -> None:
        self._loop.check()

    def start(self, on_state: Callable[[dict], None], on_unavailable: Callable[[], None],
              on_connected: Callable[[], None] | None = None) -> None:
        self._check_owner()
        if not self._stopped or self._thread is not None and self._thread.is_alive():
            raise RuntimeError('hardware UI client is already running or stopping')
        self._loop.bind()
        self._state, self._unavailable = on_state, on_unavailable
        self._connected = on_connected or (lambda: None)
        self._stopped = False
        self._spawn()

    def _spawn(self) -> None:
        self._epoch += 1
        epoch = self._epoch
        self._active = True
        self.instance = None
        self._started_at = self._last_received = self.clock()
        inbox: queue.Queue[_Result] = queue.Queue(MAX_PENDING * 2)
        outbox: queue.Queue[tuple[int, str, dict]] = queue.Queue(MAX_PENDING)
        stop = threading.Event()
        self._inbox, self._outbox, self._stop_worker = inbox, outbox, stop
        resume = self._resume_control

        def post(result: _Result) -> None:
            while not stop.is_set():
                try:
                    inbox.put(result, timeout=.1)
                    return
                except queue.Full:
                    continue

        def run() -> None:
            connection = None
            try:
                connection = self.factory()
                hello = connection.request('get_state', {})
                if resume and not stop.is_set():
                    # Consume only before transmission: a failed connection
                    # has not sent the launch action. Once sent, never replay
                    # it after a lost response or transport reconnect.
                    self._resume_control = False
                    hello = connection.request('start_control', {})
                state = validated_hardware_state(hello.get('state'))
                if hello.get('instance') != state.instance:
                    raise ProtocolError('hardware handshake instance mismatch')
                post(_Result(epoch, None, self.clock(), state))
                while not stop.is_set():
                    try:
                        serial, method, params = outbox.get(timeout=.1)
                    except queue.Empty:
                        continue
                    if stop.is_set():
                        break
                    try:
                        reply = connection.request(method, params)
                        state = validated_hardware_state(reply.get('state'))
                        if reply.get('instance') != state.instance:
                            raise ProtocolError('hardware response instance mismatch')
                        post(_Result(epoch, serial, self.clock(), state,
                                     accepted=reply['accepted'] if method == 'stop_control' else None))
                    except HardwareOperationError as error:
                        if error.code == ErrorCode.ACCESS_DENIED:
                            raise
                        post(_Result(epoch, serial, self.clock(), error.state, error))
            except Exception as error:
                post(_Result(epoch, None, self.clock(), error=error, lost=True))
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except OSError as error:
                        post(_Result(epoch, None, self.clock(), error=error, lost=True))

        self._thread = threading.Thread(target=run, name='windows-hardware-ui', daemon=True)
        self._thread.start()

    def _lost(self) -> None:
        self.instance = None
        self._active = False
        self._stop_worker.set()
        pending, self.pending = self.pending, {}
        # The model's connection epoch must change before old Futures fail.
        self._unavailable()
        for future, _ in pending.values():
            if not future.done():
                future.set_exception(BackendUnavailable('hardware service disconnected'))
        self._retry_at = self.clock() + self._backoff
        self._backoff = min(10., self._backoff * 2)

    def poll(self) -> None:
        self._check_owner()
        if self._stopped:
            return
        if not self._active:
            # A timed-out native request still owns its own pipe. Let it close
            # before creating another worker, bounding both threads and jobs.
            if not self._thread.is_alive() and self.clock() >= self._retry_at:
                self._spawn()
            return
        now = self.clock()
        if (self.instance is None and now - self._started_at >= self.timeout or
                any(now >= deadline for _, deadline in self.pending.values())):
            self._lost()
            return
        try:
            for _ in range(MAX_PENDING * 2):
                try:
                    result = self._inbox.get_nowait()
                except queue.Empty:
                    break
                if result.epoch != self._epoch:
                    continue
                now = self.clock()
                if result.lost or result.state is None or not 0 <= now - result.received <= min(self.timeout, LEASE_SECONDS):
                    raise BackendUnavailable('hardware result is stale or disconnected')
                if self.instance not in (None, result.state.instance):
                    raise ProtocolError('hardware service restarted')
                if result.serial is not None and result.serial not in self.pending:
                    raise ProtocolError('unexpected hardware operation result')
                if result.serial is not None and now >= self.pending[result.serial][1]:
                    raise BackendUnavailable('hardware operation expired')
                first = self.instance is None
                self.instance = result.state.instance
                self._last_received = now
                if first:
                    self._backoff = .5
                    self._connected()
                    if self._stopped or not self._active:
                        return
                state = result.state.to_dict()
                state.update(preferences_available=True)
                self._state(state)
                if self._stopped or not self._active:
                    return
                if result.serial is not None:
                    # State callbacks may themselves delay the owner loop.
                    # They cannot turn a now-expired result into success.
                    now = self.clock()
                    if (now >= self.pending[result.serial][1] or
                            not 0 <= now - result.received <= min(self.timeout, LEASE_SECONDS)):
                        raise BackendUnavailable('hardware completion expired')
                    future, _ = self.pending.pop(result.serial)
                    if not future.done():
                        future.set_exception(result.error) if result.error else future.set_result(result.accepted)
            if not self._thread.is_alive():
                raise BackendUnavailable('hardware worker stopped')
            if self.instance is not None and not self.pending and now - self._last_received >= IDLE_REFRESH_SECONDS:
                self.refresh()
        except (OSError, ValueError, BackendUnavailable):
            self._lost()

    def _request(self, method: str, params: dict) -> Future:
        self._check_owner()
        future = Future()
        if self._stopped or not self._active or self.instance is None or len(self.pending) >= MAX_PENDING:
            future.set_exception(BackendUnavailable('hardware service not ready or queue full'))
            return future
        try:
            if method not in SIGNATURES or method == 'session_update':
                raise ValueError('method is not a UI operation')
            self._serial += 1
            validate_request(dict(version=1, channel=CHANNEL, id=self._serial, instance=self.instance,
                                  method=method, params=params), self.instance)
            self._outbox.put_nowait((self._serial, method, dict(params)))
        except (ValueError, queue.Full) as error:
            future.set_exception(error)
            return future
        future.set_running_or_notify_cancel()
        self.pending[self._serial] = future, self.clock() + self.timeout
        return future

    def _operation(self, method: str, params: dict) -> Future:
        return self._request(method, params)

    def stop_control(self) -> Future:
        """Request graceful session exit; False means restoration is pending."""
        return self._request('stop_control', {})

    def refresh(self) -> None:
        self._check_owner()
        if self._refresh_future is None or self._refresh_future.done():
            self._refresh_future = self._request('get_state', {})

    def stop(self) -> None:
        self._check_owner()
        if not self._stopped:
            self._stopped = True
            self._lost()

    def _retry(self, fallback_mode: str | None) -> Future:
        # Native v1 always has Retry. The legacy UI fallback must not introduce
        # an implicit mode write or replay across a reconnect.
        if fallback_mode is not None and fallback_mode not in MODES:
            future = Future()
            future.set_exception(ValueError('invalid fallback mode'))
            return future
        return self._request('retry', {})
