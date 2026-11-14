"""Blocking, response-validated client side of the hardware RPC contract."""
import time
from collections.abc import Callable
from typing import Protocol

from panasonic_posture.contracts import ALL_ERROR_CODES, ErrorCode
from panasonic_posture.model import StateSnapshot
from panasonic_posture.service.engine import ServiceError
from panasonic_posture.windows.ipc.pipe import NativePipes, PIPE_NAME, PipeState
from panasonic_posture.windows.ipc.protocol import CHANNEL
from panasonic_posture.windows.ipc.frames import ProtocolError
from panasonic_posture.windows.ipc.state import (
    parse_complete_snapshot,
    parse_hardware_release_pose,
)


def validated_hardware_state(value: object) -> StateSnapshot:
    try:
        state = parse_complete_snapshot(value, incomplete='incomplete hardware state')
    except ValueError as error:
        raise ProtocolError(str(error)) from error
    if (not state.instance or
            state.sensor_source not in ('starting', 'windows-unavailable', 'windows-winrt') or
            state.supported != (state.support_tier != 'unsupported') or
            state.supported and state.sensor_source != 'windows-winrt' or
            not state.supported and state.applied is not None or state.backend == 'development'):
        raise ProtocolError('inconsistent native hardware capabilities')
    return state


class HardwareOperationError(ServiceError):
    """A validated operation failure with its authoritative state snapshot."""

    def __init__(self, code: str, state: StateSnapshot, *, detail: str | None = None,
                 hardware_release_pose: str | None = None) -> None:
        super().__init__(code, detail)
        self.state = state
        self.hardware_release_pose = hardware_release_pose


class RPCConnection(Protocol):
    def request(self, method: str, params: dict) -> dict: ...

    def close(self) -> None: ...


class HardwareConnection:
    """Blocking request client for a worker, never the GUI/main-loop thread."""

    def __init__(self, *, pipe_name: str = PIPE_NAME, pipes: NativePipes | None = None,
                 timeout: float = 5., clock: Callable[[], float] = time.monotonic) -> None:
        self.pipes = pipes or NativePipes()
        self.handle = self.pipes.connect(pipe_name)
        self.timeout, self.clock = timeout, clock
        self.instance, self.serial = None, 0

    def request(self, method: str, params: dict) -> dict:
        if self.handle is None:
            raise OSError('hardware connection is closed')
        self.serial += 1
        try:
            request = dict(
                version=1, channel=CHANNEL, id=self.serial, instance=self.instance,
                method=method, params=params,
            )
            deadline = self.clock() + self.timeout
            while True:
                result = self.pipes.write(self.handle, request)
                if result.state is PipeState.READY:
                    if self.clock() >= deadline:
                        raise TimeoutError('hardware request timed out')
                    break
                if result.state is PipeState.IDLE:
                    raise ProtocolError('hardware pipe rejected a write')
                if self.clock() >= deadline:
                    raise TimeoutError('hardware request timed out')
                time.sleep(.01)
            while self.clock() < deadline:
                result = self.pipes.read(self.handle)
                if result.state is PipeState.IDLE:
                    time.sleep(.01)
                    continue
                if result.state is PipeState.PENDING:
                    time.sleep(.005)
                    continue
                reply = result.value
                if not isinstance(reply, dict):
                    raise ProtocolError('invalid hardware response')
                if (reply.get('channel') != CHANNEL or reply.get('type') != 'response' or
                        type(reply.get('id')) is not int or reply['id'] != self.serial or
                        type(reply.get('accepted')) is not bool or 'error' not in reply or
                        reply['error'] is not None and (
                            not isinstance(reply['error'], str) or
                            reply['error'] not in ALL_ERROR_CODES) or
                        reply.get('error_detail') is not None and (
                            not isinstance(reply['error_detail'], str) or
                            not 0 < len(reply['error_detail']) <= 512)):
                    raise ProtocolError('invalid hardware response')
                if (reply['error'] is not None and reply['accepted'] or
                        reply['error'] is None and not reply['accepted'] and
                        method != 'session_update'):
                    raise ProtocolError('inconsistent hardware operation result')
                state = validated_hardware_state(reply.get('state'))
                try:
                    release_pose = parse_hardware_release_pose(reply.get('hardware_release_pose'))
                except ValueError as error:
                    raise ProtocolError(str(error)) from error
                if (not state.instance or state.instance != reply.get('instance') or
                        self.instance not in (None, state.instance)):
                    raise ProtocolError('hardware instance changed')
                self.instance = state.instance
                if reply['error'] is not None:
                    raise HardwareOperationError(
                        reply['error'], state, detail=reply.get('error_detail'),
                        hardware_release_pose=release_pose,
                    )
                return reply
            raise TimeoutError('hardware request timed out')
        except HardwareOperationError as error:
            # A rejected setting/calibration is not a broken transport. Each
            # later request is still authenticated by the server. A permission
            # denial invalidates this connection immediately.
            if error.code == ErrorCode.ACCESS_DENIED:
                self.close()
            raise
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.handle is not None:
            self.pipes.close(self.handle)
            self.handle = None


__all__ = [
    'HardwareConnection', 'HardwareOperationError', 'RPCConnection',
    'validated_hardware_state',
]
