"""LocalSystem-owned named-pipe framing and native handle operations."""
import ctypes as C
from dataclasses import dataclass
from enum import Enum
import threading

from panasonic_posture.windows.ipc.frames import MAX_FRAME, ProtocolError, decode, encode
from panasonic_posture.windows.common.security import (
    HANDLE,
    I32,
    SecurityAttributes,
    U32,
    WindowsSecurity,
)

PIPE_NAME = r'\\.\pipe\PanasonicPostureManager.Hardware.v1'
# FILE_GENERIC_READ | FILE_GENERIC_WRITE minus FILE_CREATE_PIPE_INSTANCE (4).
# GR/GW in the client ACE would accidentally let clients create server pipes.
CLIENT_ACCESS = 0x12019b
PIPE_SDDL = 'O:SYG:SYD:P(A;;GA;;;SY)(A;;0x12019b;;;IU)'
MAX_CLIENTS = 4
PIPE_TYPE_MESSAGE = 4
PIPE_READMODE_MESSAGE = 2
PIPE_REJECT_REMOTE_CLIENTS = 8
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
FILE_FLAG_OVERLAPPED = 0x40000000
ERROR_IO_PENDING = 997
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class PipeState(Enum):
    IDLE = 'idle'
    PENDING = 'pending'
    READY = 'ready'


@dataclass(frozen=True)
class PipePoll:
    """One nonblocking pipe operation poll."""

    state: PipeState
    value: object | None = None

    @classmethod
    def idle(cls) -> 'PipePoll':
        return cls(PipeState.IDLE)

    @classmethod
    def pending(cls) -> 'PipePoll':
        return cls(PipeState.PENDING)

    @classmethod
    def ready(cls, value: object | None = None) -> 'PipePoll':
        return cls(PipeState.READY, value)


class Overlapped(C.Structure):
    _fields_ = [
        ('internal', C.c_size_t),
        ('internal_high', C.c_size_t),
        ('offset', U32),
        ('offset_high', U32),
        ('event', HANDLE),
    ]


@dataclass
class _Operation:
    event: int
    overlapped: Overlapped
    buffer: object | None = None
    expected: int | None = None
    immediate: int | None = None


class NativePipes:
    """Thin overlapped-I/O boundary around one local message-mode named pipe."""

    def __init__(self, security: WindowsSecurity | None = None) -> None:
        self.security = security or WindowsSecurity()
        self.kernel = self.security.kernel
        p = C.POINTER
        fn = self.security._fn
        fn(self.kernel, 'CreateNamedPipeW', HANDLE, C.c_wchar_p, U32, U32, U32, U32,
           U32, U32, p(SecurityAttributes))
        fn(self.kernel, 'ConnectNamedPipe', I32, HANDLE, HANDLE)
        fn(self.kernel, 'DisconnectNamedPipe', I32, HANDLE)
        fn(self.kernel, 'CreateFileW', HANDLE, C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE)
        fn(self.kernel, 'SetNamedPipeHandleState', I32, HANDLE, p(U32), HANDLE, HANDLE)
        fn(self.kernel, 'PeekNamedPipe', I32, HANDLE, HANDLE, U32,
           p(U32), p(U32), HANDLE)
        fn(self.kernel, 'ReadFile', I32, HANDLE, HANDLE, U32, p(U32), HANDLE)
        fn(self.kernel, 'WriteFile', I32, HANDLE, HANDLE, U32, p(U32), HANDLE)
        fn(self.kernel, 'CreateEventW', HANDLE, HANDLE, I32, I32, C.c_wchar_p)
        fn(self.kernel, 'WaitForSingleObject', U32, HANDLE, U32)
        fn(self.kernel, 'GetOverlappedResult', I32, HANDLE, p(Overlapped), p(U32), I32)
        fn(self.kernel, 'CancelIoEx', I32, HANDLE, p(Overlapped))
        self._operations: dict[tuple[int, str], _Operation] = {}
        self._lock = threading.Lock()

    @staticmethod
    def valid_name(name: str) -> None:
        if (not isinstance(name, str) or not name.startswith('\\\\.\\pipe\\') or
                not 9 < len(name) <= 180 or any(
                    char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-'
                    for char in name[9:])):
            raise ValueError('a local pipe name is required')

    def create(self, name: str, *, first: bool) -> int:
        self.valid_name(name)
        self.security.require_system()
        descriptor = HANDLE()
        self.security._check(self.security.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            PIPE_SDDL, 1, C.byref(descriptor), None))
        try:
            attrs = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
            handle = self.kernel.CreateNamedPipeW(
                name, 3 | FILE_FLAG_OVERLAPPED | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0),
                PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_REJECT_REMOTE_CLIENTS,
                MAX_CLIENTS, MAX_FRAME, MAX_FRAME, 0, C.byref(attrs),
            )
            if handle == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            return handle
        finally:
            self.kernel.LocalFree(descriptor)

    def connect(self, name: str) -> int:
        self.valid_name(name)
        # Identification only: the server may read this token, never use the
        # client's credentials to access other resources on its behalf.
        handle = self.kernel.CreateFileW(
            name, CLIENT_ACCESS, 0, None, 3,
            0x100000 | 0x10000 | FILE_FLAG_OVERLAPPED, None,
        )
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            self.security.require_system_pipe(handle)
            mode = U32(2 | 1)
            self.security._check(
                self.kernel.SetNamedPipeHandleState(handle, C.byref(mode), None, None)
            )
            return handle
        except BaseException:
            self.close(handle)
            raise

    def _event(self) -> _Operation:
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise OSError(C.get_last_error(), 'hardware pipe event creation failed')
        return _Operation(event, Overlapped(event=event))

    @staticmethod
    def _key(handle: int, kind: str) -> tuple[int, str]:
        return handle, kind

    def _discard(self, handle: int, kind: str) -> None:
        with self._lock:
            operation = self._operations.pop(self._key(handle, kind), None)
        if operation is not None:
            self.kernel.CloseHandle(operation.event)

    def _operation(self, handle: int, kind: str) -> _Operation | None:
        with self._lock:
            return self._operations.get(self._key(handle, kind))

    def _remember(self, handle: int, kind: str, operation: _Operation) -> _Operation:
        key = self._key(handle, kind)
        with self._lock:
            if key in self._operations:
                self.kernel.CloseHandle(operation.event)
                raise OSError('hardware pipe operation was started twice')
            self._operations[key] = operation
            return operation

    def _poll_operation(self, handle: int, kind: str) -> tuple[PipePoll, _Operation | None]:
        operation = self._operation(handle, kind)
        if operation is None:
            raise OSError('hardware pipe operation is missing')
        if operation.immediate is not None:
            transfer = operation.immediate
            self._discard(handle, kind)
            return PipePoll.ready(transfer), operation
        result = self.kernel.WaitForSingleObject(operation.event, 0)
        if result == WAIT_TIMEOUT:
            return PipePoll.pending(), None
        if result != WAIT_OBJECT_0:
            self._discard(handle, kind)
            raise OSError('hardware pipe wait failed')
        transfer = U32()
        if not self.kernel.GetOverlappedResult(
                handle, C.byref(operation.overlapped), C.byref(transfer), False):
            error = C.get_last_error()
            if error == ERROR_IO_PENDING:
                return PipePoll.pending(), None
            self._discard(handle, kind)
            raise OSError(error, 'hardware pipe operation failed')
        value = transfer.value
        self._discard(handle, kind)
        return PipePoll.ready(value), operation

    def accept(self, handle: int) -> PipePoll:
        operation = self._operation(handle, 'accept')
        if operation is None:
            operation = self._event()
            operation = self._remember(handle, 'accept', operation)
            if self.kernel.ConnectNamedPipe(handle, C.byref(operation.overlapped)):
                operation.immediate = 0
            else:
                error = C.get_last_error()
                if error == 535:  # ERROR_PIPE_CONNECTED
                    operation.immediate = 0
                elif error != ERROR_IO_PENDING:
                    self._discard(handle, 'accept')
                    raise OSError(error, 'hardware pipe connection failed')
        result, _ = self._poll_operation(handle, 'accept')
        return PipePoll.ready(True) if result.state is PipeState.READY else result

    def read(self, handle: int) -> PipePoll:
        operation = self._operation(handle, 'read')
        if operation is not None:
            result, operation = self._poll_operation(handle, 'read')
            if result.state is not PipeState.READY:
                return result
            if operation is None or operation.buffer is None:
                raise ProtocolError('hardware read operation was lost')
            size_value = int(result.value)
            if not size_value:
                raise ProtocolError('empty hardware message')
            return PipePoll.ready(decode(bytes(operation.buffer.raw[:size_value])))
        available = U32()
        if not self.kernel.PeekNamedPipe(
                handle, None, 0, None, C.byref(available), None):
            error = C.get_last_error()
            if error in (109, 232, 233):  # broken pipe / no data / closing
                raise OSError(error, 'hardware pipe disconnected')
            raise OSError(error, 'hardware pipe state failed')
        if not available.value:
            return PipePoll.idle()
        operation = self._event()
        operation.buffer = C.create_string_buffer(MAX_FRAME)
        operation = self._remember(handle, 'read', operation)
        size = U32()
        if not self.kernel.ReadFile(
                handle, operation.buffer, MAX_FRAME, C.byref(size),
                C.byref(operation.overlapped)):
            error = C.get_last_error()
            if error == 234:
                self._discard(handle, 'read')
                raise ProtocolError('hardware frame exceeds the message limit')
            if error != ERROR_IO_PENDING:
                self._discard(handle, 'read')
                raise OSError(error, 'hardware pipe disconnected')
        else:
            operation.immediate = size.value
        result, completed = self._poll_operation(handle, 'read')
        if result.state is PipeState.PENDING:
            return result
        if completed is None or completed.buffer is None:
            raise ProtocolError('hardware read operation was lost')
        size_value = int(result.value)
        if not size_value:
            raise ProtocolError('empty hardware message')
        data = bytes(completed.buffer.raw[:size_value])
        return PipePoll.ready(decode(data))

    def write(self, handle: int, value: dict) -> PipePoll:
        operation = self._operation(handle, 'write')
        if operation is None:
            frame, size = encode(value), U32()
            operation = self._event()
            operation.buffer = frame
            operation.expected = len(frame)
            operation = self._remember(handle, 'write', operation)
            if not self.kernel.WriteFile(
                    handle, frame, len(frame), C.byref(size),
                    C.byref(operation.overlapped)):
                error = C.get_last_error()
                if error != ERROR_IO_PENDING:
                    self._discard(handle, 'write')
                    raise OSError(error, 'hardware pipe write failed')
            else:
                operation.immediate = size.value
        result, operation = self._poll_operation(handle, 'write')
        if result.state is PipeState.PENDING:
            return result
        if operation is None or result.value != operation.expected:
            raise OSError('hardware pipe write was incomplete')
        return PipePoll.ready()

    def reset(self, handle: int) -> None:
        self._cancel_operations(handle)
        self.kernel.DisconnectNamedPipe(handle)

    def close(self, handle: int) -> None:
        self._cancel_operations(handle)
        self.kernel.CloseHandle(handle)

    def _cancel_operations(self, handle: int) -> None:
        with self._lock:
            operations = [
                (kind, operation)
                for (candidate, kind), operation in self._operations.items()
                if candidate == handle
            ]
            for kind, _ in operations:
                self._operations.pop(self._key(handle, kind), None)
        for _, operation in operations:
            if operation.immediate is None:
                self.kernel.CancelIoEx(handle, C.byref(operation.overlapped))
                transfer = U32()
                self.kernel.GetOverlappedResult(
                    handle, C.byref(operation.overlapped), C.byref(transfer), True)
            self.kernel.CloseHandle(operation.event)


__all__ = [
    'CLIENT_ACCESS', 'MAX_CLIENTS', 'NativePipes', 'PIPE_NAME',
    'PIPE_READMODE_MESSAGE', 'PIPE_REJECT_REMOTE_CLIENTS', 'PIPE_SDDL',
    'PIPE_TYPE_MESSAGE', 'PipePoll', 'PipeState',
]
