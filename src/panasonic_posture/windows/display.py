"""Native panel rotation through the shared ownership transaction.

This adapter does not claim to control Windows' system rotation lock. Until
that port is implemented it accepts fixed modes only while Windows reports
auto-rotation disabled; tablet/native handoff fails closed. Ordinary software
preview never instantiates this adapter.
"""
from dataclasses import dataclass
import ctypes as C
import sys
import time

from ..contracts import ErrorCode
from ..session.failures import DisplayError
from ..session.transaction import DisplayState, SoftwareDisplay

U16, U32, I32 = C.c_uint16, C.c_uint32, C.c_int32


class Luid(C.Structure):
    _fields_ = [('low', U32), ('high', I32)]


class Rational(C.Structure):
    _fields_ = [('numerator', U32), ('denominator', U32)]


class SourceInfo(C.Structure):
    _fields_ = [('adapter', Luid), ('id', U32), ('mode', U32), ('status', U32)]


class TargetInfo(C.Structure):
    _fields_ = [('adapter', Luid), ('id', U32), ('mode', U32), ('technology', U32),
               ('rotation', U32), ('scaling', U32), ('refresh', Rational),
               ('scanline', U32), ('available', I32), ('status', U32)]


class PathInfo(C.Structure):
    _fields_ = [('source', SourceInfo), ('target', TargetInfo), ('flags', U32)]


class ModeInfo(C.Structure):
    # The largest SDK union member is a 48-byte, 8-aligned target mode.
    _fields_ = [('type', U32), ('id', U32), ('adapter', Luid), ('payload', C.c_uint64 * 6)]


class InfoHeader(C.Structure):
    _fields_ = [('type', U32), ('size', U32), ('adapter', Luid), ('id', U32)]


class SourceName(C.Structure):
    _fields_ = [('header', InfoHeader), ('name', U16 * 32)]


class TargetName(C.Structure):
    _fields_ = [('header', InfoHeader), ('flags', U32), ('technology', U32),
               ('manufacturer', U16), ('product', U16), ('connector', U32),
               ('friendly', U16 * 64), ('path', U16 * 128)]


class DevMode(C.Structure):
    _fields_ = [('device', U16 * 32), ('spec', U16), ('driver', U16), ('size', U16),
               ('extra', U16), ('fields', U32), ('x', I32), ('y', I32),
               ('orientation', U32), ('fixed_output', U32), ('color', U16),
               ('duplex', U16), ('y_resolution', U16), ('tt_option', U16),
               ('collate', U16), ('form', U16 * 32), ('log_pixels', U16),
               ('bits', U32), ('width', U32), ('height', U32), ('flags', U32),
               ('frequency', U32), ('icm_method', U32), ('icm_intent', U32),
               ('media', U32), ('dither', U32), ('reserved1', U32), ('reserved2', U32),
               ('panning_width', U32), ('panning_height', U32)]


def _wide(value) -> str:
    return bytes(value).decode('utf-16-le').split('\0', 1)[0]


@dataclass(frozen=True)
class Panel:
    identity: str
    source: str
    source_key: tuple[int, int, int]
    technology: int
    transform: int
    width: int
    height: int
    auto_rotation: int


def select_panel(paths: list[Panel]) -> Panel:
    internal = [path for path in paths if path.technology in (6, 11, 13, 0x80000000)]
    if len(internal) != 1:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'internal-panel-is-not-unique')
    panel = internal[0]
    if sum(path.source_key == panel.source_key for path in paths) != 1:
        raise DisplayError(ErrorCode.DISPLAY_MIRRORED)
    if not panel.identity or not panel.source or panel.transform not in range(4) or min(panel.width, panel.height) <= 0:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'invalid-internal-panel')
    return panel


class NativeDisplay:
    def __init__(self):
        if sys.platform != 'win32':
            raise OSError('native display access requires Windows')
        self.user = C.WinDLL('user32', use_last_error=True)
        for name, result, args in (
            ('GetDisplayConfigBufferSizes', I32, [U32, C.POINTER(U32), C.POINTER(U32)]),
            ('QueryDisplayConfig', I32, [U32, C.POINTER(U32), C.POINTER(PathInfo),
                                       C.POINTER(U32), C.POINTER(ModeInfo), C.c_void_p]),
            ('DisplayConfigGetDeviceInfo', I32, [C.POINTER(InfoHeader)]),
            ('EnumDisplaySettingsExW', I32, [C.c_wchar_p, U32, C.POINTER(DevMode), U32]),
            ('ChangeDisplaySettingsExW', I32, [C.c_wchar_p, C.POINTER(DevMode), C.c_void_p, U32, C.c_void_p]),
            ('GetAutoRotationState', I32, [C.POINTER(U32)]),
        ):
            function = getattr(self.user, name)
            function.restype, function.argtypes = result, args

    def _paths(self):
        for _ in range(3):
            count, modes = U32(), U32()
            error = self.user.GetDisplayConfigBufferSizes(2, C.byref(count), C.byref(modes))
            if error or not 0 < count.value <= 64 or not 0 < modes.value <= 256:
                raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, f'display-buffer-{error}')
            paths, mode_info = (PathInfo * count.value)(), (ModeInfo * modes.value)()
            error = self.user.QueryDisplayConfig(2, C.byref(count), paths, C.byref(modes), mode_info, None)
            if error == 122:
                continue
            if error:
                raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, f'display-query-{error}')
            return list(paths[:count.value])
        raise DisplayError(ErrorCode.DISPLAY_LAYOUT, 'display-topology-keeps-changing')

    def _name(self, kind, type_id, info):
        value = kind()
        value.header = InfoHeader(type_id, C.sizeof(kind), info.adapter, info.id)
        if self.user.DisplayConfigGetDeviceInfo(C.byref(value.header)):
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'display-identity-query-failed')
        return value

    def _mode(self, source):
        mode = DevMode()
        mode.size = C.sizeof(mode)
        if not self.user.EnumDisplaySettingsExW(source, 0xffffffff, C.byref(mode), 0) or mode.extra:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'display-mode-query-failed')
        return mode

    def read(self) -> Panel:
        rotation = U32()
        if not self.user.GetAutoRotationState(C.byref(rotation)):
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'rotation-state-query-failed')
        panels = []
        for path in self._paths():
            source = _wide(self._name(SourceName, 1, path.source).name)
            target = self._name(TargetName, 2, path.target)
            mode = self._mode(source)
            if mode.orientation != path.target.rotation - 1:
                raise DisplayError(ErrorCode.DISPLAY_LAYOUT, 'display-mode-query-raced')
            panels.append(Panel(_wide(target.path), source,
                (path.source.adapter.low, path.source.adapter.high, path.source.id),
                path.target.technology, mode.orientation, mode.width, mode.height, rotation.value))
        return select_panel(panels)

    def rotate(self, identity: str, previous: int, transform: int):
        if type(transform) is not int or transform not in range(4):
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'invalid-rotation')
        before = self.read()
        if before.identity != identity or before.transform != previous:
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY, 'display-changed-before-write')
        mode = self._mode(before.source)
        if (mode.orientation, mode.width, mode.height) != (before.transform, before.width, before.height):
            raise DisplayError(ErrorCode.DISPLAY_LAYOUT, 'display-mode-changed-before-write')
        if (previous ^ transform) & 1:
            mode.width, mode.height = mode.height, mode.width
        mode.orientation = transform
        # Orientation and dimensions only. In particular do not persist this
        # temporary transaction to the user's display registry or move outputs.
        mode.fields = 0x80 | 0x80000 | 0x100000
        if self.user.ChangeDisplaySettingsExW(before.source, C.byref(mode), None, 2, None):
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'display-mode-test-rejected')
        if self.read() != before:
            raise DisplayError(ErrorCode.DISPLAY_LAYOUT, 'display-changed-after-mode-test')
        result = self.user.ChangeDisplaySettingsExW(before.source, C.byref(mode), None, 0, None)
        if result:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, f'display-write-{result}')
        for _ in range(10):
            current = self.read()
            if current.identity != identity:
                raise DisplayError(ErrorCode.DISPLAY_IDENTITY)
            if current.transform == transform:
                return
            time.sleep(.05)
        raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'display-readback-failed')


class WindowsDisplay(SoftwareDisplay):
    backend_name = 'windows'

    def __init__(self, *, journal, api=None):
        self.api = api if api is not None else NativeDisplay()
        super().__init__(journal=journal)

    def _read_state(self):
        current = self.api.read()
        return DisplayState((current.identity,), current.transform)

    def _read_restore_state(self, identity):
        state = self._read_state()
        if state.identity != identity:
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY)
        return state

    def _validate(self, state, mode, transform):
        if mode not in ('laptop', 'tent', 'stand') or not self.api.read().auto_rotation & 1:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'system-rotation-lock-port-unavailable')

    def _apply_hardware(self):
        raise DisplayError(ErrorCode.DISPLAY_LOCK, 'native-rotation-handoff-unavailable')

    def _write_rotation(self, state, transform):
        self.api.rotate(state.identity[0], state.transform, transform)
