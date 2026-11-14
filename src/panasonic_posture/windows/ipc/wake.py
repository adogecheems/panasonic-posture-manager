"""Signal-only cross-session wake event published by the hardware service.

The LocalSystem service owns a Global-namespace named auto-reset event with a
null DACL.  The event carries no data and grants no authority: any local
process may signal or wait on it, and a spurious signal only costs the UI one
validated, authenticated ``get_state`` request.  State is never transported
through this object; callers must still read it through the authenticated
pipe RPC in :mod:`panasonic_posture.windows.ipc.client`.
"""
import ctypes as C

from panasonic_posture.windows.common.security import (
    HANDLE,
    I32,
    SecurityAttributes,
    U32,
    WindowsSecurity,
)

WAKE_EVENT_NAME = r'Global\PanasonicPostureManager.Wake.v1'
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


class WakeEvent:
    """Own one named auto-reset event handle in the creating process."""

    def __init__(self, *, name: str = WAKE_EVENT_NAME,
                 security: WindowsSecurity | None = None) -> None:
        self.name = name
        self.security = security or WindowsSecurity()
        self.kernel = self.security.kernel
        fn = self.security._fn
        fn(self.kernel, 'CreateEventW', HANDLE, HANDLE, I32, I32, C.c_wchar_p)
        fn(self.kernel, 'OpenEventW', HANDLE, U32, I32, C.c_wchar_p)
        fn(self.kernel, 'SetEvent', I32, HANDLE)
        fn(self.kernel, 'WaitForSingleObject', U32, HANDLE, U32)
        self.handle: int | None = None

    def create(self) -> None:
        """Create the event with a null DACL so any console user can wait.

        The null DACL is deliberate: the object is signal-only.  Waking the
        observer grants nothing beyond one validated state read.
        """
        if self.handle is not None:
            raise OSError('hardware wake event is already open')
        attrs = SecurityAttributes(C.sizeof(SecurityAttributes), None, False)
        handle = self.kernel.CreateEventW(C.byref(attrs), False, False, self.name)
        if not handle:
            raise C.WinError(C.get_last_error())
        self.handle = handle

    def open(self) -> None:
        if self.handle is not None:
            raise OSError('hardware wake event is already open')
        handle = self.kernel.OpenEventW(SYNCHRONIZE, False, self.name)
        if not handle:
            raise C.WinError(C.get_last_error())
        self.handle = handle

    def publish(self) -> None:
        if self.handle is None:
            raise OSError('hardware wake event is closed')
        if not self.kernel.SetEvent(self.handle):
            raise C.WinError(C.get_last_error())

    def wait(self, timeout_ms: int) -> int:
        if self.handle is None:
            raise OSError('hardware wake event is closed')
        return self.kernel.WaitForSingleObject(self.handle, timeout_ms)

    def close(self) -> None:
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


__all__ = [
    'SYNCHRONIZE',
    'WAIT_OBJECT_0',
    'WAIT_TIMEOUT',
    'WAKE_EVENT_NAME',
    'WakeEvent',
]
