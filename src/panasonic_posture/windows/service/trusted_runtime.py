"""Pin and verify the installation used for privileged and session processes.

No code from an ordinary user's directory may become a service import path.
Checks use open handles, reject reparse points, inspect effective DACL grants
and retain no-delete-sharing pins until the host and its children stop.
"""
import ctypes as C
from dataclasses import dataclass
import os
from pathlib import Path
import sys
from typing import Protocol

from panasonic_posture.windows.common.security import WindowsSecurity, U32, I32, HANDLE

SYSTEM = 'S-1-5-18'
ADMINS = 'S-1-5-32-544'
WRITE_RIGHTS = 0x500d0156
HARDWARE_EXECUTABLE = 'runtime/python-hardware.exe'
HARDWARE_PTH = 'runtime/python-hardware._pth'


@dataclass(frozen=True)
class Ace:
    sid: str
    mask: int
    flags: int = 0
    kind: int = 0


def protected_acl(owner: str, aces: list[Ace] | None, *, ancestor: bool = False,
                  trusted: frozenset[str] = frozenset({SYSTEM, ADMINS})) -> bool:
    if owner not in trusted or aces is None or len(aces) > 128:
        return False
    for ace in aces:
        if (not isinstance(ace, Ace) or type(ace.mask) is not int or not 0 <= ace.mask <= 0xffffffff or
                type(ace.flags) is not int or not 0 <= ace.flags <= 255 or type(ace.kind) is not int):
            return False
        if ace.flags & 8 or ace.kind == 1:  # Inherit-only or deny cannot grant current access.
            continue
        if ace.kind != 0:
            return False  # Do not infer access from conditional/object ACEs.
        dangerous = WRITE_RIGHTS & ~4 if ancestor else WRITE_RIGHTS
        if ace.sid not in trusted and ace.mask & dangerous:
            return False
    return True


class AttributeTag(C.Structure):
    _fields_ = [('attributes', U32), ('tag', U32)]


class AclSize(C.Structure):
    _fields_ = [('count', U32), ('used', U32), ('free', U32)]


class AceHeader(C.Structure):
    _fields_ = [('kind', C.c_ubyte), ('flags', C.c_ubyte), ('size', C.c_uint16)]


class RuntimeVerifier(Protocol):
    def pin(self, path: Path, *, ancestor: bool = False) -> None: ...
    def close(self) -> None: ...


class NativeProtectedTree:
    def __init__(self) -> None:
        self.security = WindowsSecurity()
        self.kernel, self.advapi = self.security.kernel, self.security.advapi
        self.handles: list[int] = []
        fn = self.security._fn
        fn(self.kernel, 'CreateFileW', HANDLE, C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE)
        fn(self.kernel, 'GetFileType', U32, HANDLE)
        fn(self.kernel, 'GetFileInformationByHandleEx', I32, HANDLE, U32, HANDLE, U32)
        fn(self.advapi, 'GetAclInformation', I32, HANDLE, HANDLE, U32, U32)
        fn(self.advapi, 'GetAce', I32, HANDLE, U32, C.POINTER(HANDLE))
        fn(self.advapi, 'IsValidSid', I32, HANDLE)
        fn(self.advapi, 'GetLengthSid', U32, HANDLE)
        fn(self.advapi, 'LookupAccountNameW', I32, C.c_wchar_p, C.c_wchar_p, HANDLE,
           C.POINTER(U32), C.c_wchar_p, C.POINTER(U32), C.POINTER(U32))
        sid, size, domain, domain_size, kind = C.create_string_buffer(68), U32(68), C.create_unicode_buffer(256), U32(256), U32()
        if not self.advapi.LookupAccountNameW(None, r'NT SERVICE\TrustedInstaller', sid,
                                            C.byref(size), domain, C.byref(domain_size), C.byref(kind)):
            raise OSError('cannot identify trusted installation owner')
        self.trusted = frozenset({SYSTEM, ADMINS, self.security._sid(sid)})

    def _acl(self, handle: int) -> tuple[str, list[Ace] | None]:
        owner, dacl, descriptor = HANDLE(), HANDLE(), HANDLE()
        code = self.advapi.GetSecurityInfo(handle, 1, 5, C.byref(owner), None,
                                          C.byref(dacl), None, C.byref(descriptor))
        try:
            if code:
                raise OSError(code, 'cannot read runtime security descriptor')
            owner_sid = self.security._sid(owner)
            if not dacl:
                return owner_sid, None
            size = AclSize()
            self.security._check(self.advapi.GetAclInformation(dacl, C.byref(size), C.sizeof(size), 2))
            if size.count > 128:
                raise OSError('runtime ACL exceeds the inspection limit')
            aces = []
            for index in range(size.count):
                pointer = HANDLE()
                self.security._check(self.advapi.GetAce(dacl, index, C.byref(pointer)))
                header = C.cast(pointer, C.POINTER(AceHeader)).contents
                if header.size < 8:
                    raise OSError('invalid runtime ACL entry')
                mask = C.cast(pointer.value + 4, C.POINTER(U32))[0]
                if header.flags & 8 or header.kind == 1:
                    aces.append(Ace('', mask, header.flags, header.kind))
                    continue
                if header.kind != 0:
                    raise OSError('unsupported runtime ACL entry')
                sid = pointer.value + 8
                if (not self.advapi.IsValidSid(sid) or
                        self.advapi.GetLengthSid(sid) > header.size - 8):
                    raise OSError('invalid runtime ACL principal')
                aces.append(Ace(self.security._sid(sid), mask, header.flags, header.kind))
            return owner_sid, aces
        finally:
            self.kernel.LocalFree(descriptor)

    def pin(self, path: Path, *, ancestor: bool = False) -> None:
        # READ_CONTROL | FILE_READ_ATTRIBUTES, no delete sharing. Do not follow
        # a junction/symlink before examining the opened object's attributes.
        handle = self.kernel.CreateFileW(str(path), 0x20080, 3, None, 3, 0x02200000, None)
        if handle is None or handle == C.c_void_p(-1).value:
            raise OSError(C.get_last_error(), 'cannot pin runtime path')
        try:
            attributes = AttributeTag()
            if (self.kernel.GetFileType(handle) != 1 or
                    not self.kernel.GetFileInformationByHandleEx(handle, 9, C.byref(attributes), C.sizeof(attributes)) or
                    attributes.attributes & 0x400 or ancestor and not attributes.attributes & 0x10):
                raise PermissionError('runtime path is not an ordinary local file or directory')
            owner, aces = self._acl(handle)
            if not protected_acl(owner, aces, ancestor=ancestor, trusted=self.trusted):
                raise PermissionError('runtime path is owned or writable by an untrusted principal')
            self.handles.append(handle)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise

    def close(self) -> None:
        failed = []
        for handle in reversed(self.handles):
            if not self.kernel.CloseHandle(handle):
                failed.append(handle)
        self.handles = failed
        if failed:
            raise OSError('runtime path pins could not be closed')


class TrustedRuntime:
    def __init__(self, root: Path, *, verifier: RuntimeVerifier | None = None) -> None:
        self.root = Path(root).absolute()
        self.verifier = verifier if verifier is not None else NativeProtectedTree()
        self.closed = False
        try:
            if '..' in self.root.parts or self.root.drive.startswith('\\\\'):
                raise ValueError('a local installation path is required')
            for parent in reversed(self.root.parents):
                self.verifier.pin(parent, ancestor=True)
            self.verifier.pin(self.root)
            count = 0
            def failed_walk(error: OSError) -> None:
                raise error
            for directory, children, files in os.walk(self.root, followlinks=False, onerror=failed_walk):
                children.sort()
                for name in children + sorted(files):
                    count += 1
                    if count > 20000:
                        raise ValueError('runtime tree exceeds the inspection limit')
                    self.verifier.pin(Path(directory) / name)
            for name in (HARDWARE_EXECUTABLE, 'runtime/Lib/os.py',
                         HARDWARE_PTH,
                         'app/panasonic_posture/windows/service/daemon.py',
                         'app/panasonic_posture/windows/session/agent.py',
                         'data/profiles/cfqv8.json',
                         'data/profiles/cfqv9.json',
                         'data/profiles/cfqv1.json'):
                if not (self.root / name).is_file():
                    raise ValueError(f'incomplete installed runtime: {name}')
            paths = list((self.root / 'runtime').glob('python*._pth'))
            unexpected = {
                path.name for path in paths
                if path.name not in {'python._pth', 'pythonw._pth', 'python-hardware._pth'}
            }
            if unexpected:
                raise ValueError('a DLL-specific search path can override the hardware runtime')
            hardware_pth = self.root / HARDWARE_PTH
            if hardware_pth.read_text(encoding='utf-8').splitlines() != ['Lib', 'DLLs', '../app']:
                raise ValueError('privileged runtime search path is not isolated')
        except BaseException:
            self.close()
            raise

    @classmethod
    def current(cls) -> 'TrustedRuntime':
        root = Path(__file__).absolute().parents[3]
        if (Path(sys.executable).absolute() != root / HARDWARE_EXECUTABLE or
                not sys.flags.isolated or not sys.dont_write_bytecode):
            raise PermissionError('hardware host requires its installed isolated interpreter')
        return cls(root)

    def agent_arguments(self, journal: Path, *, stop_event: str | None = None) -> list[str]:
        if self.closed:
            raise OSError('installed runtime is closed')
        if not journal.is_absolute() or any(char in str(journal) for char in '\0\r\n'):
            raise ValueError('an absolute per-user journal path is required')
        arguments = [str(self.root / HARDWARE_EXECUTABLE), '-B', '-I', '-m',
                     'panasonic_posture.windows.session.agent', '--journal', str(journal)]
        if stop_event is not None:
            from panasonic_posture.windows.common.stop_event import validate_name
            validate_name(stop_event)
            arguments += ['--stop-event', stop_event]
        return arguments

    def close(self) -> None:
        self.closed = True
        self.verifier.close()
