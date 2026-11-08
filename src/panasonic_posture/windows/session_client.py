"""Blocking session RPC adapter for the shared runtime's worker threads.

At most two independent channels keep display/state work from occupying the
lease worker's pipe. Every channel pins the same service instance. A change
invalidates all in-flight results before any old display receipt can be sent
to a replacement service. The host still authenticates the exact agent PID.
"""
from dataclasses import dataclass, field
from collections.abc import Callable
from _thread import LockType
import threading

from ..contracts import ErrorCode, ALL_ERROR_CODES
from ..model import StateSnapshot
from ..session.failures import ServiceError, FailureKind
from .hardware_ipc import HardwareConnection, HardwareOperationError, RPCConnection, validated_hardware_state
from .protocol import ProtocolError


@dataclass
class _Channel:
    connection: RPCConnection | None = None
    epoch: int = -1
    lock: LockType = field(default_factory=threading.Lock)


class WindowsSessionClient:
    def __init__(self, *, factory: Callable[[], RPCConnection] = HardwareConnection,
                 unattended: Callable[[], bool] = lambda: False) -> None:
        self.factory, self.unattended = factory, unattended
        self._local = threading.local()
        self._lock = threading.Lock()
        self._channels: list[_Channel] = []
        self._epoch = 0
        self._instance: str | None = None
        self._closed = False

    def _channel(self) -> tuple[_Channel, int]:
        with self._lock:
            if self._closed:
                raise OSError('session client is closed')
            channel = getattr(self._local, 'channel', None)
            if channel is None:
                if len(self._channels) >= 2:
                    raise OSError('session RPC worker limit reached')
                channel = _Channel()
                self._channels.append(channel)
                self._local.channel = channel
            return channel, self._epoch

    def _check(self, reply: dict, epoch: int) -> StateSnapshot:
        state = validated_hardware_state(reply.get('state'))
        if reply.get('instance') != state.instance or type(reply.get('accepted')) is not bool:
            raise ProtocolError('invalid session response identity')
        with self._lock:
            if self._closed or epoch != self._epoch:
                raise ServiceError(ErrorCode.INTERNAL, 'stale session channel', kind=FailureKind.REJECTED)
            if self._instance is None:
                self._instance = state.instance
            elif state.instance != self._instance:
                self._epoch += 1
                self._instance = None
                raise ServiceError(ErrorCode.INTERNAL, 'hardware service restarted', kind=FailureKind.REJECTED)
        return state

    @staticmethod
    def _drop(channel: _Channel) -> None:
        if channel.connection is not None:
            try:
                channel.connection.close()
            finally:
                channel.connection = None

    def _request(self, method: str, params: dict) -> tuple[StateSnapshot, bool]:
        channel, epoch = self._channel()
        with channel.lock:
            try:
                with self._lock:
                    if self._closed or epoch != self._epoch:
                        raise ServiceError(ErrorCode.INTERNAL, 'stale session channel', kind=FailureKind.REJECTED)
                if channel.epoch != epoch:
                    self._drop(channel)
                if channel.connection is None:
                    channel.connection = self.factory()
                    channel.epoch = epoch
                    hello = channel.connection.request('get_state', {})
                    state = self._check(hello, epoch)
                    if method == 'get_state':
                        return state, hello['accepted']
                # Pin the epoch again before transmitting a write. An old
                # result on another channel may have detected a restart.
                with self._lock:
                    if self._closed or epoch != self._epoch:
                        raise ServiceError(ErrorCode.INTERNAL, 'stale session channel', kind=FailureKind.REJECTED)
                reply = channel.connection.request(method, params)
                return self._check(reply, epoch), reply['accepted']
            except HardwareOperationError as error:
                try:
                    self._check(dict(state=error.state.to_dict(), instance=error.state.instance,
                                     accepted=False), epoch)
                except ServiceError:
                    self._drop(channel)
                    raise
                except ProtocolError as invalid:
                    self._drop(channel)
                    raise ServiceError(ErrorCode.INTERNAL, str(invalid), kind=FailureKind.REJECTED) from invalid
                if error.code == ErrorCode.ACCESS_DENIED:
                    self._drop(channel)
                raise ServiceError(error.code, error.detail) from error
            except ProtocolError as error:
                self._drop(channel)
                raise ServiceError(ErrorCode.INTERNAL, str(error), kind=FailureKind.REJECTED) from error
            except ServiceError:
                self._drop(channel)
                raise
            except (OSError, ValueError):
                self._drop(channel)
                raise

    def get_state(self) -> StateSnapshot:
        return self._request('get_state', {})[0]

    def session_update(self, generation: int, ready: bool, error_code: str | None = None,
                       error_detail: str | None = None) -> None:
        if (type(generation) is not int or not 0 <= generation < 2**63 or type(ready) is not bool or
                error_code is not None and (not isinstance(error_code, str) or error_code not in ALL_ERROR_CODES | {''}) or
                error_detail is not None and (not isinstance(error_detail, str) or len(error_detail) > 512) or
                ready and (error_code or error_detail)):
            raise ValueError('invalid session feedback')
        unattended = self.unattended()
        if type(unattended) is not bool:
            raise ValueError('invalid unattended context')
        self._request('session_update', dict(generation=generation, ready=ready and not unattended,
            error_code=error_code or None, error_detail=error_detail or None, unattended=unattended))

    def confirm_tent(self, epoch: int) -> None:
        self._request('confirm_prompt', dict(prompt='tent', epoch=epoch))

    def confirm_stand(self, epoch: int) -> None:
        self._request('confirm_prompt', dict(prompt='stand', epoch=epoch))

    def wake_screen(self, generation: int, serial: int) -> None:
        raise ServiceError(ErrorCode.INTERNAL, 'native touch-backed wake is not implemented', kind=FailureKind.UNAVAILABLE)

    def close(self) -> None:
        # SessionRuntime closes its executors before the process shell closes
        # this client. The locks also make explicit concurrent shutdown safe.
        with self._lock:
            self._closed = True
            self._epoch += 1
            channels = list(self._channels)
        for channel in channels:
            with channel.lock:
                self._drop(channel)
