"""Read-only observation of the current Windows shell interaction mode.

Posture changes are reported through the laptop/slate GPIO indicator.  Windows
owns the user's "ask / do not switch / switch automatically" choice, so this
module never calls the ImmersiveShell mode setter.
"""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes
import logging
import sys
import uuid

DESKTOP = 0
TABLET = 1
VALID_MODES = frozenset((DESKTOP, TABLET))

_COINIT_APARTMENTTHREADED = 0x2
_RPC_E_CHANGED_MODE = -2147417850
_CLSCTX_LOCAL_SERVER = 0x4
_IMMERSIVE_SHELL_KEY = r'Software\Microsoft\Windows\CurrentVersion\ImmersiveShell'
_TABLET_MODE_PREFERENCE = 'ConvertibleSlateModePromptPreference'
_FUNCTYPE = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)


class TabletModeError(OSError):
    pass


log = logging.getLogger(__name__)


def read_prompt_preference() -> int | None:
    """Read the user's Windows-owned response choice without changing it."""

    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _IMMERSIVE_SHELL_KEY, 0,
                            winreg.KEY_QUERY_VALUE) as key:
            value, kind = winreg.QueryValueEx(key, _TABLET_MODE_PREFERENCE)
    except FileNotFoundError:
        return None
    if kind != winreg.REG_DWORD or type(value) is not int or value not in (0, 1, 2):
        log.warning('Windows tablet-mode preference has an unknown value')
        return None
    return value


class _Guid(C.Structure):
    _fields_ = [('data1', C.c_uint32), ('data2', C.c_uint16),
                ('data3', C.c_uint16), ('data4', C.c_ubyte * 8)]

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
        raise TabletModeError('Windows shell mode requires Windows')
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
            query_service = _FUNCTYPE(
                C.c_long, C.c_void_p, C.POINTER(_Guid), C.POINTER(_Guid),
                C.POINTER(C.c_void_p),
            )(service_vtable[3])
            controller = C.c_void_p()
            result = query_service(
                service, C.byref(_IID_TABLET_MODE_CONTROLLER),
                C.byref(_IID_TABLET_MODE_CONTROLLER), C.byref(controller),
            )
            if not _succeeded(result):
                raise TabletModeError(f'tablet mode observer unavailable: 0x{result & 0xffffffff:08x}')
            try:
                yield C.cast(controller, C.POINTER(C.POINTER(C.c_void_p)))[0], controller
            finally:
                release = _FUNCTYPE(wintypes.ULONG, C.c_void_p)(
                    C.cast(controller, C.POINTER(C.POINTER(C.c_void_p)))[0][2]
                )
                release(controller)
        finally:
            release = _FUNCTYPE(wintypes.ULONG, C.c_void_p)(service_vtable[2])
            release(service)


class WindowsShellMode:
    """Observe the shell result; never force a desktop/tablet switch."""

    def read(self) -> int:
        with _controller() as (vtable, controller):
            get_mode = _FUNCTYPE(C.c_long, C.c_void_p, C.POINTER(C.c_int))(vtable[3])
            mode = C.c_int(-1)
            result = get_mode(controller, C.byref(mode))
            if not _succeeded(result) or mode.value not in VALID_MODES:
                raise TabletModeError(
                    f'shell mode query failed: hr=0x{result & 0xffffffff:08x} mode={mode.value}'
                )
            return mode.value


# Preserve the existing import name while making its read-only contract clear.
WindowsTabletMode = WindowsShellMode

__all__ = [
    'DESKTOP', 'TABLET', 'TabletModeError', 'WindowsShellMode',
    'WindowsTabletMode', 'read_prompt_preference',
]
