"""Minimal Windows 10 tablet-mode controller with transactional callers.

The public UWP API exposes the current user interaction mode but does not
provide a setter.  Windows 10's ImmersiveShell exposes the same controller
used by the shell and OOBE to switch between desktop and tablet mode.  This
module keeps the undocumented COM boundary isolated and strictly validates
both the mode and the post-write read-back.
"""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes
import sys
import time
import uuid

DESKTOP = 0
TABLET = 1
VALID_MODES = frozenset((DESKTOP, TABLET))

_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = -2147417850
_CLSCTX_LOCAL_SERVER = 0x4
_TRIGGER_API = 0x4


class TabletModeError(OSError):
    pass


class _Guid(C.Structure):
    _fields_ = [('data1', wintypes.DWORD), ('data2', wintypes.WORD),
                ('data3', wintypes.WORD), ('data4', C.c_ubyte * 8)]

    @classmethod
    def parse(cls, value: str):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


_CLSID_IMMERSIVE_SHELL = _Guid.parse('{C2F03A33-21F5-47FA-B4BB-156362A2F239}')
_IID_SERVICE_PROVIDER = _Guid.parse('{6D5140C1-7436-11CE-8034-00AA006009FA}')
_IID_TABLET_MODE_CONTROLLER = _Guid.parse('{4FDA780A-ACD2-41F7-B4F2-EBE674C9BF2A}')


def _succeeded(result: int) -> bool:
    return result >= 0


@contextmanager
def _apartment():
    if sys.platform != 'win32':
        raise TabletModeError('Windows tablet mode requires Windows')
    ole32 = C.WinDLL('ole32', use_last_error=True)
    ole32.CoInitializeEx.argtypes = [C.c_void_p, wintypes.DWORD]
    ole32.CoInitializeEx.restype = C.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    initialized = ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    if not _succeeded(initialized) and initialized != _RPC_E_CHANGED_MODE:
        raise TabletModeError(f'CoInitializeEx failed: 0x{initialized & 0xffffffff:08x}')
    try:
        yield ole32
    finally:
        if _succeeded(initialized):
            ole32.CoUninitialize()


@contextmanager
def _controller():
    with _apartment() as ole32:
        ole32.CoCreateInstance.argtypes = [
            C.POINTER(_Guid), C.c_void_p, wintypes.DWORD,
            C.POINTER(_Guid), C.POINTER(C.c_void_p),
        ]
        ole32.CoCreateInstance.restype = C.c_long
        service = C.c_void_p()
        result = ole32.CoCreateInstance(
            C.byref(_CLSID_IMMERSIVE_SHELL), None, _CLSCTX_LOCAL_SERVER,
            C.byref(_IID_SERVICE_PROVIDER), C.byref(service),
        )
        if not _succeeded(result):
            raise TabletModeError(f'ImmersiveShell unavailable: 0x{result & 0xffffffff:08x}')
        try:
            service_vtable = C.cast(service, C.POINTER(C.POINTER(C.c_void_p)))[0]
            query_service = C.WINFUNCTYPE(
                C.c_long, C.c_void_p, C.POINTER(_Guid), C.POINTER(_Guid),
                C.POINTER(C.c_void_p),
            )(service_vtable[3])
            controller = C.c_void_p()
            result = query_service(
                service, C.byref(_IID_TABLET_MODE_CONTROLLER),
                C.byref(_IID_TABLET_MODE_CONTROLLER), C.byref(controller),
            )
            if not _succeeded(result):
                raise TabletModeError(f'tablet mode controller unavailable: 0x{result & 0xffffffff:08x}')
            try:
                yield C.cast(controller, C.POINTER(C.POINTER(C.c_void_p)))[0], controller
            finally:
                release = C.WINFUNCTYPE(wintypes.ULONG, C.c_void_p)(
                    C.cast(controller, C.POINTER(C.POINTER(C.c_void_p)))[0][2]
                )
                release(controller)
        finally:
            release = C.WINFUNCTYPE(wintypes.ULONG, C.c_void_p)(service_vtable[2])
            release(service)


class WindowsTabletMode:
    """Read and verify the current shell interaction mode."""

    def read(self) -> int:
        with _controller() as (vtable, controller):
            get_mode = C.WINFUNCTYPE(C.c_long, C.c_void_p, C.POINTER(C.c_int))(vtable[3])
            mode = C.c_int(-1)
            result = get_mode(controller, C.byref(mode))
            if not _succeeded(result) or mode.value not in VALID_MODES:
                raise TabletModeError(
                    f'tablet mode query failed: hr=0x{result & 0xffffffff:08x} mode={mode.value}'
                )
            return mode.value

    def write(self, mode: int) -> None:
        if type(mode) is not int or mode not in VALID_MODES:
            raise ValueError('tablet mode must be desktop (0) or tablet (1)')
        with _controller() as (vtable, controller):
            set_mode = C.WINFUNCTYPE(C.c_long, C.c_void_p, C.c_int, C.c_int)(vtable[4])
            result = set_mode(controller, mode, _TRIGGER_API)
            if not _succeeded(result):
                raise TabletModeError(f'tablet mode write failed: 0x{result & 0xffffffff:08x}')
        current = None
        for _ in range(10):
            current = self.read()
            if current == mode:
                return
            time.sleep(.05)
        raise TabletModeError(
            f'tablet mode read-back mismatch: {current} != {mode}'
        )


__all__ = ['DESKTOP', 'TABLET', 'TabletModeError', 'WindowsTabletMode']
