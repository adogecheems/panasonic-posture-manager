"""Native panel rotation through the shared ownership transaction.

This adapter owns only the internal panel direction, automatic-rotation
lock and their crash-recovery journal. Windows shell state is reported through
the convertible-slate device as an independent operation. Physical tablet
handoff relinquishes panel rotation to Windows without changing the user's
shell policy. Ordinary UI processes never use this port.
"""
from dataclasses import dataclass
import ctypes as C
from ctypes import wintypes
import logging
import sys
import time

from panasonic_posture.contracts import ErrorCode
from panasonic_posture.model import HardwareStatus, StateSnapshot, Target
from panasonic_posture.session.failures import DisplayError
from panasonic_posture.session.transaction import DisplayState, SoftwareDisplay, desired_transform

log = logging.getLogger(__name__)
U16, U32, I32 = C.c_uint16, C.c_uint32, C.c_int32
INTERNAL_TECHNOLOGIES = frozenset((6, 11, 13, 0x80000000))


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
    internal = [path for path in paths if path.technology in INTERNAL_TECHNOLOGIES]
    if len(internal) != 1:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'internal-panel-is-not-unique')
    panel = internal[0]
    if sum(path.source_key == panel.source_key for path in paths) != 1:
        raise DisplayError(ErrorCode.DISPLAY_MIRRORED)
    if not panel.identity or not panel.source or panel.transform not in range(4) or min(panel.width, panel.height) <= 0:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'invalid-internal-panel')
    return panel


def select_external_only(paths: list[Panel]) -> tuple[str, ...]:
    """Return a stable external layout that must be preserved without writes."""
    if any(path.technology in INTERNAL_TECHNOLOGIES for path in paths):
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, 'internal-panel-still-present')
    external = list(paths)
    if not external:
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'no-active-display')
    if any(not path.identity or not path.source or path.transform not in range(4)
           or min(path.width, path.height) <= 0 for path in external):
        raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'invalid-external-display')
    identities = tuple(sorted(path.identity for path in external))
    if len(set(identities)) != len(identities):
        raise DisplayError(ErrorCode.DISPLAY_IDENTITY, 'external-display-identity-not-unique')
    return identities


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
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.GetProcAddress.argtypes = [C.c_void_p, C.c_void_p]
        kernel.GetProcAddress.restype = C.c_void_p
        address = kernel.GetProcAddress(self.user._handle, 2507)
        self._set_auto_rotation = (
            C.WINFUNCTYPE(wintypes.BOOL, wintypes.BOOL)(address)
            if address else None
        )

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

    def panels(self) -> list[Panel]:
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
        return panels

    def read(self) -> Panel:
        return select_panel(self.panels())

    def external_identity(self) -> tuple[str, ...]:
        return select_external_only(self.panels())

    def lock(self, locked: bool) -> None:
        """Acquire or restore the global automatic-rotation lock.

        ``user32!#2507`` is the shell's ``SetAutoRotation(BOOL)`` entry point;
        Windows has no documented ``user32`` setter for this state.  Keep the
        undocumented boundary here, verify every read-back, and fail closed if
        the export or state transition is unavailable.
        """

        if type(locked) is not bool:
            raise ValueError('rotation lock must be boolean')
        before = self.read()
        if bool(before.auto_rotation & 1) == locked:
            return
        if self._set_auto_rotation is None:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'rotation-lock-export-unavailable')
        if not self._set_auto_rotation(not locked):
            get_error = getattr(C, 'get_last_error', None)
            error = get_error() if get_error is not None else None
            raise DisplayError(ErrorCode.DISPLAY_LOCK, f'rotation-lock-write-{error}')
        for _ in range(20):
            current = self.read()
            if current.identity != before.identity:
                raise DisplayError(ErrorCode.DISPLAY_IDENTITY, 'display-changed-during-lock')
            if bool(current.auto_rotation & 1) == locked:
                return
            time.sleep(.05)
        raise DisplayError(ErrorCode.DISPLAY_LOCK, 'rotation-lock-readback-failed')

    def rotate(self, identity: str, previous: int, transform: int):
        if type(transform) is not int or transform not in range(4):
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'invalid-rotation')
        before = self.read()
        if before.identity != identity or before.transform != previous:
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY, 'display-changed-before-write')
        if not before.auto_rotation & 1:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'native-auto-rotation-took-over')
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
            if not current.auto_rotation & 1:
                raise DisplayError(ErrorCode.DISPLAY_LOCK, 'native-auto-rotation-took-over')
            if current.transform == transform:
                return
            time.sleep(.05)
        raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'display-readback-failed')


class ExternalOnlyDisplay:
    """Preserve a docked external layout while posture/slate logic continues."""
    backend_name = 'windows-external-only'
    supports_software_tablet = False

    def __init__(self, api):
        self.api = api
        self.identity: tuple[str, ...] | None = None

    def _verify(self) -> tuple[str, ...]:
        current = self.api.external_identity()
        if self.identity is not None and current != self.identity:
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY, 'external-layout-changed')
        self.identity = current
        return current

    def apply(self, target: Target | dict) -> None:
        mode = target.get('mode') if isinstance(target, dict) else target.mode
        if mode == 'unavailable':
            return
        if mode not in {'laptop', 'tent', 'stand', 'hardware'}:
            raise DisplayError(ErrorCode.DISPLAY_UNKNOWN_MODE, str(mode or 'empty'))
        self._verify()

    def hold_orientation(self) -> None:
        self._verify()

    @staticmethod
    def needs_refresh(target: Target) -> bool:
        return target.mode in {'laptop', 'tent', 'stand'}

    def refresh(self, state: StateSnapshot) -> None:
        del state
        self._verify()

    def restore(self, normal: bool = False, *, release_lock: bool = False,
                keep_journal: bool = False) -> None:
        del normal, release_lock, keep_journal
        self.identity = None


class WindowsDisplay(SoftwareDisplay):
    backend_name = 'windows'
    lock_is_global = True

    def __init__(self, *, journal, api=None, hardware_query=None):
        self.api = api if api is not None else NativeDisplay()
        self.hardware_query = hardware_query
        self._adopt_native_handoff = False
        super().__init__(journal=journal)

    @property
    def native_handoff_active(self) -> bool:
        """Whether Windows currently owns the physical tablet direction."""

        return self._adopt_native_handoff

    def _read_state(self):
        current = self.api.read()
        return DisplayState(
            (current.identity,), current.transform,
            lock=bool(current.auto_rotation & 1),
            lock_supported=callable(getattr(self.api, 'lock', None)),
        )

    def _read_restore_state(self, identity):
        state = self._read_state()
        if state.identity != identity:
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY)
        return state

    def _read_global_lock(self):
        return bool(self.api.read().auto_rotation & 1)

    def _write_lock(self, state, locked):
        self.api.lock(locked)

    def _write_original_lock(self, state, policy):
        del state
        if policy not in ('true', 'false'):
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'invalid-original-lock-policy')
        self.api.lock(policy == 'true')

    def _validate(self, state, mode, transform):
        del transform
        if mode not in ('laptop', 'tent', 'stand', 'hold'):
            raise DisplayError(ErrorCode.DISPLAY_UNKNOWN_MODE, mode)
        if not state.lock_supported:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'rotation-lock-port-unavailable')
        self._require_software_ownership()

    def _hardware(self):
        if self.hardware_query is None:
            return False  # Explicitly injected display-only fixture.
        hardware, status = self.hardware_query()
        if type(hardware) is not bool or status != HardwareStatus.OK:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'physical-state-unavailable')
        return hardware

    def _require_software_ownership(self):
        if self._hardware():
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'physical-tablet-took-over')

    def apply(self, target: Target | dict) -> None:
        mode = target.get('mode') if isinstance(target, dict) else target.mode
        if mode != 'hardware':
            self._adopt_native_handoff = False
        try:
            super().apply(target)
            if mode in ('laptop', 'tent', 'stand'):
                self.verify_software_target(target)
        except (DisplayError, OSError, ValueError) as error:
            self._rollback()
            if (isinstance(error, DisplayError)
                    and error.code == ErrorCode.DISPLAY_LOCK
                    and error.detail == 'physical-tablet-took-over'):
                # Hardware posture changed while an older fixed-mode
                # transaction was finishing. The rollback relinquished the
                # software journal; complete the same request as a hardware
                # handoff so the session can verify the newer snapshot.
                self._apply_hardware()
                self._adopt_native_handoff = True
                return
            raise
        else:
            if mode == 'hardware':
                self._adopt_native_handoff = True

    def verify_software_target(self, target: Target | dict) -> None:
        """Verify a fixed mode still owns the lock and direction after CSM."""

        mode = target.get('mode') if isinstance(target, dict) else target.mode
        if mode not in ('laptop', 'tent', 'stand'):
            return
        current = self._read_state()
        self._require_software_ownership()
        if not current.lock:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'rotation-lock-took-over')
        if not self.owned or current.identity != tuple(self.owned['spec']):
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY)
        if current.transform != (2 if mode == 'tent' else 0):
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'fixed-rotation-changed-before-receipt')

    @staticmethod
    def needs_refresh(target: Target) -> bool:
        return target.mode in ('laptop', 'tent', 'stand')

    def refresh(self, state) -> None:
        """Reclaim a fixed target after a delayed Windows shell response.

        The session runtime passes a current, generation-fenced snapshot and
        checks it again after this operation. Windows can re-enable automatic
        rotation several seconds after CSM notification without changing the
        requested mode, so ongoing display work must verify the lock while the
        service still reports a healthy non-tablet state.
        """

        target = state.target
        mode = target.mode
        if mode not in ('laptop', 'tent', 'stand'):
            return
        if state.hardware is not False or state.hardware_status != HardwareStatus.OK:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'physical-state-unavailable')
        transform = desired_transform(mode)
        state_now = self._read_transaction_state()
        if not state_now.lock_supported:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'rotation-lock-port-unavailable')
        self._adopt_native_handoff = False
        self._held_orientation = None
        try:
            if not self.owned:
                self.owned = self._journal_document(state_now)
                self._persist()
            self._write_owned_lock(state_now, True)
            state_now = self._read_state()
            if not self.owned or tuple(self.owned['spec']) != state_now.identity:
                raise DisplayError(ErrorCode.DISPLAY_IDENTITY)
            self._write_owned_rotation(state_now, transform)
            state_now = self._read_state()
            if not self.owned or tuple(self.owned['spec']) != state_now.identity:
                raise DisplayError(ErrorCode.DISPLAY_IDENTITY)
            if not state_now.lock:
                raise DisplayError(ErrorCode.DISPLAY_LOCK, 'rotation-lock-took-over')
            if state_now.transform != transform:
                raise DisplayError(
                    ErrorCode.DISPLAY_TRANSFORM,
                    'fixed-rotation-changed-during-refresh',
                )
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def hold_orientation(self) -> None:
        """Observe, but do not interrupt, Windows' post-tablet direction handoff."""

        if not self._adopt_native_handoff:
            super().hold_orientation()
            return
        state = self._read_transaction_state()
        # Field logs showed Windows overrides the rotation lock when the
        # convertible-slate metric is released, then rotates the panel to the
        # native desktop direction. Re-asserting the lock on every hold tick
        # closes that override as early as possible without touching the
        # direction, so the settled target still performs at most one visible
        # rotation through the normal apply path.
        self._write_owned_lock(state, True)
        state = self._read_state()
        self._held_orientation = state
        if not self.owned:
            self.owned = self._journal_document(state)
            self._persist()
        elif self.owned.get('written_transform') is None:
            self.owned['original_transform'] = state.transform
            self._persist()

    def restore(self, normal: bool = False, *, release_lock: bool = False,
                keep_journal: bool = False) -> None:
        self._adopt_native_handoff = False
        if self.owned and any(
                field in self.owned
                for field in ('original_shell_mode', 'written_shell_mode',
                              'pending_shell_mode')):
            # Older journals coupled shell ownership to the display. Current
            # recovery owns rotation only and must leave the shell where the
            # user or Windows put it.
            for field in ('original_shell_mode', 'written_shell_mode',
                          'pending_shell_mode'):
                self.owned.pop(field, None)
            self._persist()
        if self.owned and self._hardware():
            self._restore_for_hardware()
            return
        if self.owned and not release_lock and any(
                self.owned.get(field) is not None
                for field in ('written_transform', 'pending_transform')):
            state = self._read_state()
            if (tuple(self.owned.get('spec') or ()) == state.identity
                    and state.lock_supported and not self._is_locked(state)):
                # Reacquire only while the journal shows that this transaction
                # owned the direction. This lets a failed fixed-mode write put
                # its own rotation back, then restores the user's lock policy.
                self._write_owned_lock(state, True)
        super().restore(
            normal=normal,
            release_lock=release_lock,
            keep_journal=keep_journal,
        )

    def _relinquish_native_rotation(self):
        """Do not overwrite a direction owned by Windows' active rotator."""

        owned = self.owned
        if (not owned or
                all(owned.get(field) is None
                    for field in ('written_transform', 'pending_transform'))):
            return
        state = self._read_state()
        if tuple(owned['spec']) != state.identity:
            return
        if not state.lock_supported or self._is_locked(state):
            return
        # The software transaction always keeps Windows' rotator locked while
        # it owns a fixed direction. Seeing it unlocked means the platform has
        # taken over; persist that handoff before restoring the lock policy.
        owned['written_transform'] = None
        owned.pop('pending_transform', None)
        self._persist()

    def _restore_for_hardware(self) -> None:
        """Discard software orientation while preserving physical priority."""

        state = self._read_state()
        if tuple(self.owned['spec']) != state.identity:
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY)
        if state.lock_supported and not self._is_locked(state):
            self._write_owned_lock(state, True)
            state = self._read_state()
        self.owned['written_transform'] = None
        self.owned.pop('pending_transform', None)
        self._persist()
        if state.lock_supported:
            self._write_owned_lock(state, False)
        self.journal.clear()
        self.owned = None

    def _write_rotation(self, state, transform):
        self._require_software_ownership()
        if not self.api.read().auto_rotation & 1:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'native-auto-rotation-took-over')
        log.warning('writing internal panel rotation %s -> %s', state.transform, transform)
        self.api.rotate(state.identity[0], state.transform, transform)
