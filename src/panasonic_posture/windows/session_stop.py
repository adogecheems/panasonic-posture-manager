"""A SYSTEM-owned one-way stop signal: the child can wait, never set it."""
import ctypes as C
import re
from uuid import uuid4

from .security import WindowsSecurity, SecurityAttributes, HANDLE, U32, I32

PREFIX = 'Global\\PanasonicPostureManager.Stop.'


def validate_name(name: str) -> None:
    if not isinstance(name, str) or not name.startswith(PREFIX) or not re.fullmatch('[0-9a-f]{32}', name[len(PREFIX):]):
        raise ValueError('invalid native stop-event name')


class NativeStopSignal:
    def __init__(self, security: WindowsSecurity | None = None) -> None:
        self.security = security if security is not None else WindowsSecurity()
        self.kernel = self.security.kernel
        self.handle = None
        self.writable = False
        self.name = ''
        fn = self.security._fn
        fn(self.kernel, 'CreateEventW', HANDLE, C.POINTER(SecurityAttributes), I32, I32, C.c_wchar_p)
        fn(self.kernel, 'OpenEventW', HANDLE, U32, I32, C.c_wchar_p)
        fn(self.kernel, 'SetEvent', I32, HANDLE)
        fn(self.kernel, 'WaitForSingleObject', U32, HANDLE, U32)

    @classmethod
    def create(cls, sid: str, *, security: WindowsSecurity | None = None) -> 'NativeStopSignal':
        if not isinstance(sid, str) or len(sid) > 184 or not re.fullmatch(r'S-1-(?:\d+-){1,14}\d+', sid):
            raise ValueError('a canonical user SID is required')
        signal = cls(security)
        signal.security.require_system()
        signal.name = PREFIX + uuid4().hex
        descriptor = HANDLE()
        # SYNCHRONIZE | READ_CONTROL only for the selected console user.
        sddl = f'O:SYG:SYD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;0x120000;;;{sid})'
        signal.security._check(signal.security.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl, 1, C.byref(descriptor), None))
        try:
            attributes = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
            C.set_last_error(0)
            signal.handle = signal.kernel.CreateEventW(C.byref(attributes), True, False, signal.name)
            if not signal.handle:
                raise OSError(C.get_last_error(), 'cannot create native stop signal')
            if C.get_last_error() == 183:
                raise PermissionError('native stop signal name already exists')
            signal.writable = True
            return signal
        except BaseException:
            signal.close()
            raise
        finally:
            signal.kernel.LocalFree(descriptor)

    @classmethod
    def open(cls, name: str, *, security: WindowsSecurity | None = None) -> 'NativeStopSignal':
        validate_name(name)
        signal = cls(security)
        signal.name = name
        signal.handle = signal.kernel.OpenEventW(0x120000, False, name)
        if not signal.handle:
            raise OSError(C.get_last_error(), 'cannot open native stop signal')
        try:
            signal.security.require_system_object(signal.handle, 'session stop signal')
            return signal
        except BaseException:
            signal.close()
            raise

    def is_set(self) -> bool:
        if self.handle is None:
            raise OSError('native stop signal is closed')
        value = self.kernel.WaitForSingleObject(self.handle, 0)
        if value not in (0, 258):
            raise OSError('cannot read native stop signal')
        return value == 0

    def set(self) -> None:
        if self.handle is None or not self.writable:
            raise PermissionError('this stop handle is read-only or closed')
        self.security._check(self.kernel.SetEvent(self.handle))

    def close(self) -> None:
        if self.handle is not None:
            if not self.kernel.CloseHandle(self.handle):
                raise OSError('native stop signal could not be closed')
            self.handle = None
