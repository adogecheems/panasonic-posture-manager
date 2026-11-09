"""Windows token/console authentication; never consume an identity from JSON."""
import ctypes as C
import os
import sys

from .hardware_host import ConsoleIdentity

U32, I32, HANDLE = C.c_uint32, C.c_int32, C.c_void_p


class Luid(C.Structure):
    _fields_ = [('low', U32), ('high', I32)]


class TokenStatistics(C.Structure):
    _fields_ = [('token', Luid), ('authentication', Luid), ('expires', C.c_int64),
                ('type', U32), ('level', U32), ('charged', U32), ('available', U32),
                ('groups', U32), ('privileges', U32), ('modified', Luid)]


class SessionInfo(C.Structure):
    _fields_ = [('session', U32), ('state', I32), ('flags', I32),
                ('station', C.c_wchar * 33), ('user', C.c_wchar * 21), ('domain', C.c_wchar * 18),
                ('logon', C.c_int64), ('connect', C.c_int64), ('disconnect', C.c_int64),
                ('last_input', C.c_int64), ('current', C.c_int64),
                ('in_bytes', U32), ('out_bytes', U32), ('in_frames', U32), ('out_frames', U32),
                ('in_compressed', U32), ('out_compressed', U32)]


class ExtendedInfo(C.Structure):
    _fields_ = [('level', U32), ('data', SessionInfo)]


class SecurityAttributes(C.Structure):
    _fields_ = [('length', U32), ('descriptor', HANDLE), ('inherit', I32)]


class AuthenticationError(PermissionError):
    pass


def foreground_matches(peer: ConsoleIdentity, console: tuple[str, int, int], *,
                       active: bool, unlocked: bool) -> bool:
    return (isinstance(peer, ConsoleIdentity) and active is True and unlocked is True and
            (peer.sid, peer.logon_id, peer.session_id) == console and peer.session_id > 0)


class WindowsSecurity:
    def __init__(self) -> None:
        if sys.platform != 'win32':
            raise OSError('Windows security APIs require Windows')
        self.kernel = C.WinDLL('kernel32', use_last_error=True)
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        self.wts = C.WinDLL('wtsapi32', use_last_error=True)
        self._bind()

    def _fn(self, dll, name, result, *args):
        fn = getattr(dll, name)
        fn.restype, fn.argtypes = result, args
        return fn

    def _bind(self):
        p = C.POINTER
        self._fn(self.kernel, 'GetCurrentProcess', HANDLE)
        self._fn(self.kernel, 'GetCurrentThread', HANDLE)
        self._fn(self.kernel, 'CloseHandle', I32, HANDLE)
        self._fn(self.kernel, 'LocalFree', HANDLE, HANDLE)
        self._fn(self.kernel, 'OpenProcess', HANDLE, U32, I32, U32)
        self._fn(self.kernel, 'GetExitCodeProcess', I32, HANDLE, p(U32))
        self._fn(self.kernel, 'GetProcessTimes', I32, HANDLE, p(C.c_uint64), p(C.c_uint64), p(C.c_uint64), p(C.c_uint64))
        self._fn(self.kernel, 'GetNamedPipeClientProcessId', I32, HANDLE, p(U32))
        self._fn(self.kernel, 'WTSGetActiveConsoleSessionId', U32)
        self._fn(self.advapi, 'OpenProcessToken', I32, HANDLE, U32, p(HANDLE))
        self._fn(self.advapi, 'OpenThreadToken', I32, HANDLE, U32, I32, p(HANDLE))
        self._fn(self.advapi, 'GetTokenInformation', I32, HANDLE, U32, HANDLE, U32, p(U32))
        self._fn(self.advapi, 'ConvertSidToStringSidW', I32, HANDLE, p(HANDLE))
        self._fn(self.advapi, 'ImpersonateNamedPipeClient', I32, HANDLE)
        self._fn(self.advapi, 'RevertToSelf', I32)
        self._fn(self.advapi, 'ConvertStringSecurityDescriptorToSecurityDescriptorW', I32, C.c_wchar_p, U32, p(HANDLE), p(U32))
        self._fn(self.advapi, 'GetSecurityInfo', U32, HANDLE, U32, U32, p(HANDLE), HANDLE, HANDLE, HANDLE, p(HANDLE))
        self._fn(self.wts, 'WTSQueryUserToken', I32, U32, p(HANDLE))
        self._fn(self.wts, 'WTSQuerySessionInformationW', I32, HANDLE, U32, U32, p(HANDLE), p(U32))
        self._fn(self.wts, 'WTSFreeMemory', None, HANDLE)

    def _check(self, value):
        if not value:
            raise AuthenticationError(C.get_last_error(), 'Windows identity could not be verified')
        return value

    def _sid(self, sid):
        value = HANDLE()
        self._check(self.advapi.ConvertSidToStringSidW(sid, C.byref(value)))
        try:
            return C.wstring_at(value)
        finally:
            self.kernel.LocalFree(value)

    def _token_info(self, token, kind):
        size = U32()
        self.advapi.GetTokenInformation(token, kind, None, 0, C.byref(size))
        if not 0 < size.value <= 65536:
            raise AuthenticationError('invalid token information size')
        buffer = C.create_string_buffer(size.value)
        self._check(self.advapi.GetTokenInformation(token, kind, buffer, size, C.byref(size)))
        return buffer

    def token_identity(self, token) -> tuple[str, int, int]:
        user = self._token_info(token, 1)
        sid = self._sid(C.cast(user, C.POINTER(HANDLE))[0])
        stats = C.cast(self._token_info(token, 10), C.POINTER(TokenStatistics)).contents
        logon = ((stats.authentication.high & 0xffffffff) << 32) | stats.authentication.low
        session = C.cast(self._token_info(token, 12), C.POINTER(U32))[0]
        return sid, logon, session

    def require_system(self) -> None:
        token = HANDLE()
        self._check(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, C.byref(token)))
        try:
            if self.token_identity(token)[0] != 'S-1-5-18':
                raise AuthenticationError('hardware host must run as LocalSystem')
        finally:
            self.kernel.CloseHandle(token)

    def require_unprivileged(self) -> None:
        """The native desktop adapter must use an ordinary interactive token."""
        token = HANDLE()
        self._check(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 8, C.byref(token)))
        try:
            identity = self.token_identity(token)
            elevation = self._token_info(token, 20)  # TokenElevation
            if (identity[0] == 'S-1-5-18' or identity[2] in (0, 0xffffffff) or
                    C.sizeof(elevation) != C.sizeof(U32) or C.cast(elevation, C.POINTER(U32))[0] != 0):
                raise AuthenticationError('native session agent requires a non-elevated user token')
        finally:
            self.kernel.CloseHandle(token)

    def process_identity(self, pid: int) -> ConsoleIdentity:
        process = self._check(self.kernel.OpenProcess(0x1000, False, pid))
        token = HANDLE()
        try:
            self._check(self.advapi.OpenProcessToken(process, 8, C.byref(token)))
            sid, logon, session = self.token_identity(token)
            created, exited, kernel, user = (C.c_uint64() for _ in range(4))
            self._check(self.kernel.GetProcessTimes(process, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)))
            code = U32()
            self._check(self.kernel.GetExitCodeProcess(process, C.byref(code)))
            if code.value != 259:
                raise AuthenticationError('client process has exited')
            return ConsoleIdentity(sid, logon, session, pid, created.value)
        finally:
            if token:
                self.kernel.CloseHandle(token)
            self.kernel.CloseHandle(process)

    def peer(self, pipe: int) -> ConsoleIdentity:
        self._check(self.advapi.ImpersonateNamedPipeClient(pipe))
        token = HANDLE()
        try:
            self._check(self.advapi.OpenThreadToken(self.kernel.GetCurrentThread(), 8, True, C.byref(token)))
            impersonated = self.token_identity(token)
        finally:
            if token:
                self.kernel.CloseHandle(token)
            # Microsoft requires terminating after a failed revert. Never let
            # this worker return to service work under the client's token.
            if not self.advapi.RevertToSelf():
                os._exit(70)
        pid = U32()
        self._check(self.kernel.GetNamedPipeClientProcessId(pipe, C.byref(pid)))
        identity = self.process_identity(pid.value)
        if impersonated != (identity.sid, identity.logon_id, identity.session_id) or not self.is_active(identity):
            raise AuthenticationError('client is not the unlocked foreground console user')
        return identity

    def is_active(self, peer: ConsoleIdentity) -> bool:
        token, buffer, size = HANDLE(), HANDLE(), U32()
        try:
            session = self.kernel.WTSGetActiveConsoleSessionId()
            if session in (0, 0xffffffff) or session != peer.session_id:
                return False
            if self.process_identity(peer.process_id) != peer:
                return False
            self._check(self.wts.WTSQueryUserToken(session, C.byref(token)))
            console = self.token_identity(token)
            self._check(self.wts.WTSQuerySessionInformationW(None, session, 25, C.byref(buffer), C.byref(size)))
            if size.value < C.sizeof(ExtendedInfo):
                return False
            info = C.cast(buffer, C.POINTER(ExtendedInfo)).contents
            return foreground_matches(peer, console,
                active=info.level == 1 and info.data.session == session and info.data.state == 0,
                unlocked=info.data.flags == 1)
        except (OSError, ValueError):
            return False
        finally:
            if token:
                self.kernel.CloseHandle(token)
            if buffer:
                self.wts.WTSFreeMemory(buffer)

    def require_system_pipe(self, handle: int) -> None:
        self.require_system_object(handle, 'hardware pipe')

    def require_system_object(self, handle: int, description: str) -> None:
        owner, descriptor = HANDLE(), HANDLE()
        error = self.advapi.GetSecurityInfo(handle, 6, 1, C.byref(owner), None, None, None, C.byref(descriptor))
        if error:
            raise AuthenticationError(error, f'cannot verify {description} owner')
        try:
            if self._sid(owner) != 'S-1-5-18':
                raise AuthenticationError(f'{description} is not owned by LocalSystem')
        finally:
            self.kernel.LocalFree(descriptor)
