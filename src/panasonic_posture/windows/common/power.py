"""Signal suspend boundaries without a window, display access or RPC work."""
from collections.abc import Callable
import ctypes as C

HANDLE, U32 = C.c_void_p, C.c_uint32
Callback = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(U32, HANDLE, U32, HANDLE)


class SubscribeParameters(C.Structure):
    _fields_ = [('callback', Callback), ('context', HANDLE)]


class SuspendResumeSource:
    # Retain the ctypes trampoline if native unregistration fails.
    _live: dict[int, 'SuspendResumeSource'] = {}

    def __init__(self, notify: Callable[[bool], None], *, api=None) -> None:
        self.api = api if api is not None else C.WinDLL('powrprof', use_last_error=True, winmode=0x800)
        self.notify = notify
        self.closed = False
        self.error: str | None = None
        self.handle = HANDLE()
        self.callback = Callback(self._callback)
        self.parameters = SubscribeParameters(self.callback, None)
        self.api.PowerRegisterSuspendResumeNotification.restype = U32
        self.api.PowerRegisterSuspendResumeNotification.argtypes = (U32, HANDLE, C.POINTER(HANDLE))
        self.api.PowerUnregisterSuspendResumeNotification.restype = U32
        self.api.PowerUnregisterSuspendResumeNotification.argtypes = (HANDLE,)
        self._live[id(self)] = self
        try:
            code = self.api.PowerRegisterSuspendResumeNotification(2, C.byref(self.parameters), C.byref(self.handle))
            if code or not self.handle.value:
                raise OSError(code, 'service suspend observation failed')
        except BaseException:
            self.close()
            raise

    def _callback(self, context, kind, setting) -> int:
        if self.closed or kind not in (4, 7, 0x12):
            return 0
        try:
            self.notify(kind == 4)
            self.error = None
            return 0
        except Exception as error:
            self.error = f'invalid suspend callback: {type(error).__name__}'
            return 13

    def close(self) -> None:
        self.closed = True
        if self.handle.value:
            code = self.api.PowerUnregisterSuspendResumeNotification(self.handle)
            if code:
                raise OSError(code, 'service suspend observation cleanup failed')
            self.handle = HANDLE()
        self._live.pop(id(self), None)
