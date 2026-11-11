"""LocalSystem-owned named-pipe framing and native handle operations."""
import ctypes as C

from .protocol import MAX_FRAME, ProtocolError, decode, encode
from .security import HANDLE, I32, SecurityAttributes, U32, WindowsSecurity

PIPE_NAME = r'\\.\pipe\PanasonicPostureManager.Hardware.v1'
# FILE_GENERIC_READ | FILE_GENERIC_WRITE minus FILE_CREATE_PIPE_INSTANCE (4).
# GR/GW in the client ACE would accidentally let clients create server pipes.
CLIENT_ACCESS = 0x12019b
PIPE_SDDL = 'O:SYG:SYD:P(A;;GA;;;SY)(A;;0x12019b;;;IU)'
MAX_CLIENTS = 4


class NativePipes:
    """Thin ctypes boundary around one local message-mode named pipe."""

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
        fn(self.kernel, 'ReadFile', I32, HANDLE, HANDLE, U32, p(U32), HANDLE)
        fn(self.kernel, 'WriteFile', I32, HANDLE, HANDLE, U32, p(U32), HANDLE)

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
                name, 3 | (0x80000 if first else 0), 4 | 2 | 1 | 8,
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
            name, CLIENT_ACCESS, 0, None, 3, 0x100000 | 0x10000, None,
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

    def accept(self, handle: int) -> bool:
        if self.kernel.ConnectNamedPipe(handle, None):
            return True
        error = C.get_last_error()
        if error == 535:  # ERROR_PIPE_CONNECTED
            return True
        if error in (536, 232):  # LISTENING / NO_DATA
            return False
        raise OSError(error, 'hardware pipe connection failed')

    def read(self, handle: int) -> dict | None:
        data, size = C.create_string_buffer(MAX_FRAME), U32()
        if not self.kernel.ReadFile(handle, data, MAX_FRAME, C.byref(size), None):
            error = C.get_last_error()
            if error == 232:  # empty nonblocking pipe
                return None
            if error == 234:
                raise ProtocolError('hardware frame exceeds the message limit')
            raise OSError(error, 'hardware pipe disconnected')
        if not size.value:
            raise ProtocolError('empty hardware message')
        return decode(data.raw[:size.value])

    def write(self, handle: int, value: dict) -> None:
        frame, size = encode(value), U32()
        if (not self.kernel.WriteFile(handle, frame, len(frame), C.byref(size), None) or
                size.value != len(frame)):
            raise OSError(C.get_last_error(), 'hardware pipe write failed or client stalled')

    def reset(self, handle: int) -> None:
        self.kernel.DisconnectNamedPipe(handle)

    def close(self, handle: int) -> None:
        self.kernel.CloseHandle(handle)


__all__ = [
    'CLIENT_ACCESS', 'MAX_CLIENTS', 'NativePipes', 'PIPE_NAME', 'PIPE_SDDL',
]
