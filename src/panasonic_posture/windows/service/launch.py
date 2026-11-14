"""Launch one ordinary console-user agent suspended from a pinned runtime.

Only trusted host code calls this module. No executable, command, token, PID
or journal path comes from an RPC caller. The host registers the returned
identity before explicitly resuming the child. All WTS/creation work belongs
on a single bounded worker, outside the service engine's control loop.
"""
from collections.abc import Callable
from contextlib import ExitStack
import ctypes as C
from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
from typing import Protocol
from uuid import UUID

from panasonic_posture.windows.common.identity import ConsoleIdentity, ConsoleUser
from panasonic_posture.windows.common.security import (
    WindowsSecurity,
    ExtendedInfo,
    U32,
    I32,
    HANDLE,
    AuthenticationError,
)
from panasonic_posture.windows.common.stop_event import NativeStopSignal
from panasonic_posture.windows.service.trusted_runtime import TrustedRuntime

CREATE_FLAGS = 0x08000404  # NO_WINDOW | UNICODE_ENVIRONMENT | SUSPENDED


class StartupInfo(C.Structure):
    _fields_ = [('size', U32), ('reserved', C.c_wchar_p), ('desktop', C.c_wchar_p),
                ('title', C.c_wchar_p)] + [(name, U32) for name in (
                    'x', 'y', 'width', 'height', 'chars_x', 'chars_y', 'fill', 'flags')] + [
                ('show', C.c_uint16), ('reserved_size', C.c_uint16), ('reserved_data', HANDLE),
                ('stdin', HANDLE), ('stdout', HANDLE), ('stderr', HANDLE)]


class ProcessInfo(C.Structure):
    _fields_ = [('process', HANDLE), ('thread', HANDLE), ('pid', U32), ('tid', U32)]


class JobLimits(C.Structure):
    _fields_ = [('process_time', C.c_int64), ('job_time', C.c_int64), ('flags', U32),
                ('minimum', C.c_size_t), ('maximum', C.c_size_t), ('active', U32),
                ('affinity', C.c_size_t), ('priority', U32), ('scheduling', U32)]


class ExtendedLimits(C.Structure):
    _fields_ = [('basic', JobLimits), ('io', C.c_uint64 * 6), ('process_memory', C.c_size_t),
                ('job_memory', C.c_size_t), ('peak_process', C.c_size_t), ('peak_job', C.c_size_t)]


class LaunchAPI(Protocol):
    def require_system(self) -> None: ...
    def console_token(self) -> tuple[int | None, ConsoleUser | None]: ...
    def duplicate(self, token: int) -> int: ...
    def environment(self, token: int) -> int: ...
    def destroy_environment(self, environment: int) -> None: ...
    def journal(self, token: int) -> Path: ...
    def job(self) -> int: ...
    def start(self, token: int, arguments: list[str], environment: int, directory: Path) -> tuple[int, int, int]: ...
    def assign(self, job: int, process: int) -> None: ...
    def identity(self, pid: int) -> ConsoleIdentity: ...
    def process_elevated(self, process: int) -> bool: ...
    def is_active(self, peer: ConsoleIdentity) -> bool: ...
    def resume(self, thread: int) -> None: ...
    def alive(self, process: int) -> bool: ...
    def terminate(self, process: int) -> None: ...
    def wait(self, process: int, timeout: int) -> bool: ...
    def close(self, handle: int) -> None: ...


class StopSignal(Protocol):
    name: str
    def is_set(self) -> bool: ...
    def set(self) -> None: ...
    def close(self) -> None: ...


class NativeLaunchAPI:
    def __init__(self) -> None:
        self.security = WindowsSecurity()
        self.security.require_system()
        if C.sizeof(HANDLE) != 8:
            raise OSError('the native host requires a 64-bit runtime')
        self.kernel, self.advapi, self.wts = self.security.kernel, self.security.advapi, self.security.wts
        self.userenv = C.WinDLL('userenv', use_last_error=True, winmode=0x800)
        self.shell = C.WinDLL('shell32', use_last_error=True, winmode=0x800)
        self.ole = C.WinDLL('ole32', use_last_error=True, winmode=0x800)
        fn, p = self.security._fn, C.POINTER
        fn(self.advapi, 'DuplicateTokenEx', I32, HANDLE, U32, HANDLE, U32, U32, p(HANDLE))
        fn(self.userenv, 'CreateEnvironmentBlock', I32, p(HANDLE), HANDLE, I32)
        fn(self.userenv, 'DestroyEnvironmentBlock', I32, HANDLE)
        fn(self.shell, 'SHGetKnownFolderPath', I32, HANDLE, U32, HANDLE, p(HANDLE))
        fn(self.ole, 'CoTaskMemFree', None, HANDLE)
        fn(self.kernel, 'CreateJobObjectW', HANDLE, HANDLE, C.c_wchar_p)
        fn(self.kernel, 'SetInformationJobObject', I32, HANDLE, U32, HANDLE, U32)
        fn(self.kernel, 'AssignProcessToJobObject', I32, HANDLE, HANDLE)
        fn(self.kernel, 'WaitForSingleObject', U32, HANDLE, U32)
        fn(self.kernel, 'ResumeThread', U32, HANDLE)
        fn(self.kernel, 'TerminateProcess', I32, HANDLE, U32)
        fn(self.advapi, 'CreateProcessAsUserW', I32, HANDLE, C.c_wchar_p, C.c_wchar_p,
           HANDLE, HANDLE, I32, U32, HANDLE, C.c_wchar_p, p(StartupInfo), p(ProcessInfo))

    def require_system(self) -> None:
        self.security.require_system()

    def _elevated(self, token: int) -> bool:
        data = self.security._token_info(token, 20)
        if C.sizeof(data) != C.sizeof(U32):
            raise AuthenticationError('unknown token elevation')
        value = C.cast(data, C.POINTER(U32))[0]
        if value not in (0, 1):
            raise AuthenticationError('unknown token elevation')
        return value == 1

    def console_token(self) -> tuple[int | None, ConsoleUser | None]:
        session = self.kernel.WTSGetActiveConsoleSessionId()
        if session in (0, 0xffffffff):
            return None, None
        token, buffer, size = HANDLE(), HANDLE(), U32()
        try:
            if not self.wts.WTSQueryUserToken(session, C.byref(token)):
                if C.get_last_error() == 1008:  # ERROR_NO_TOKEN during logoff.
                    return None, None
                raise AuthenticationError(C.get_last_error(), 'console token unavailable')
            identity = self.security.token_identity(token)
            if identity[0] == 'S-1-5-18' or identity[2] != session:
                raise AuthenticationError('console token identity mismatch')
            if self._elevated(token):
                data = self.security._token_info(token, 19)  # TokenLinkedToken
                if C.sizeof(data) != C.sizeof(HANDLE):
                    raise AuthenticationError('ordinary linked console token unavailable')
                linked = C.cast(data, C.POINTER(HANDLE))[0]
                if not linked:
                    raise AuthenticationError('ordinary linked console token unavailable')
                try:
                    if self.security.token_identity(linked) != identity or self._elevated(linked):
                        raise AuthenticationError('linked token is not the same ordinary logon')
                except BaseException:
                    self.close(linked)
                    raise
                try:
                    self.close(token.value)
                except BaseException:
                    self.close(linked)
                    raise
                token = HANDLE(linked)
            self.security._check(self.wts.WTSQuerySessionInformationW(
                None, session, 25, C.byref(buffer), C.byref(size)))
            if not buffer or size.value < C.sizeof(ExtendedInfo):
                raise AuthenticationError('incomplete console session information')
            info = C.cast(buffer, C.POINTER(ExtendedInfo)).contents
            if info.level != 1 or info.data.session != session or info.data.flags not in (0, 1):
                raise AuthenticationError('unknown console session information')
            if info.data.state != 0 or self.kernel.WTSGetActiveConsoleSessionId() != session:
                return None, None
            user = ConsoleUser(*identity, unlocked=info.data.flags == 1)
            handle, token.value = token.value, None
            return handle, user
        finally:
            if buffer:
                self.wts.WTSFreeMemory(buffer)
            if token:
                self.close(token.value)

    def duplicate(self, token: int) -> int:
        duplicate = HANDLE()
        # ASSIGN_PRIMARY | DUPLICATE | IMPERSONATE | QUERY. Known-folder
        # resolution additionally needs IMPERSONATE, beyond process creation.
        self.security._check(self.advapi.DuplicateTokenEx(token, 0xf, None, 2, 1, C.byref(duplicate)))
        return duplicate.value

    def environment(self, token: int) -> int:
        block = HANDLE()
        self.security._check(self.userenv.CreateEnvironmentBlock(C.byref(block), token, False))
        if not block:
            raise OSError('empty user environment block')
        return block.value

    def destroy_environment(self, environment: int) -> None:
        self.security._check(self.userenv.DestroyEnvironmentBlock(environment))

    def journal(self, token: int) -> Path:
        # Resolve LocalAppData for this token. The host only passes the path;
        # the ordinary child, not SYSTEM, creates and writes the journal.
        guid = (C.c_ubyte * 16).from_buffer_copy(UUID('f1b32785-6fba-4fcf-9d55-7b8e7f157091').bytes_le)
        value = HANDLE()
        try:
            code = self.shell.SHGetKnownFolderPath(C.byref(guid), 0, token, C.byref(value))
            if code or not value:
                raise OSError(code, 'user state directory unavailable')
            directory = Path(C.wstring_at(value))
            if not directory.is_absolute():
                raise OSError('user state directory is not absolute')
            return directory / 'PanasonicPostureManager' / 'native-display.json'
        finally:
            if value:
                self.ole.CoTaskMemFree(value)

    def job(self) -> int:
        job = self.security._check(self.kernel.CreateJobObjectW(None, None))
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            self.security._check(self.kernel.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)))
            return job
        except BaseException:
            self.close(job)
            raise

    def start(self, token: int, arguments: list[str], environment: int, directory: Path) -> tuple[int, int, int]:
        startup = StartupInfo()
        startup.size, startup.desktop = C.sizeof(startup), r'winsta0\default'
        startup.flags, startup.show = 1, 0
        info = ProcessInfo()
        command = C.create_unicode_buffer(subprocess.list2cmdline(arguments))
        self.security._check(self.advapi.CreateProcessAsUserW(token, arguments[0], command,
            None, None, False, CREATE_FLAGS, environment, str(directory), C.byref(startup), C.byref(info)))
        if not info.process or not info.thread or not info.pid:
            with ExitStack() as cleanup:
                if info.process:
                    cleanup.callback(self.close, info.process)
                if info.thread:
                    cleanup.callback(self.close, info.thread)
                if info.process and self.alive(info.process):
                    self.terminate(info.process)
                    if not self.wait(info.process, 2000):
                        raise OSError('incomplete child creation did not stop')
            raise OSError('process creation returned incomplete handles')
        return info.process, info.thread, info.pid

    def assign(self, job: int, process: int) -> None:
        self.security._check(self.kernel.AssignProcessToJobObject(job, process))

    def identity(self, pid: int) -> ConsoleIdentity:
        return self.security.process_identity(pid)

    def process_elevated(self, process: int) -> bool:
        token = HANDLE()
        self.security._check(self.advapi.OpenProcessToken(process, 8, C.byref(token)))
        try:
            return self._elevated(token)
        finally:
            self.close(token.value)

    def is_active(self, peer: ConsoleIdentity) -> bool:
        return self.security.is_active(peer)

    def resume(self, thread: int) -> None:
        if self.kernel.ResumeThread(thread) != 1:
            raise OSError('session thread was not suspended exactly once')

    def alive(self, process: int) -> bool:
        return not self.wait(process, 0)

    def wait(self, process: int, timeout: int) -> bool:
        result = self.kernel.WaitForSingleObject(process, timeout)
        if result not in (0, 258):
            raise OSError('session process wait failed')
        return result == 0

    def terminate(self, process: int) -> None:
        self.security._check(self.kernel.TerminateProcess(process, 1))

    def close(self, handle: int) -> None:
        if not self.kernel.CloseHandle(handle):
            raise OSError('session resource could not be closed')


class ManagedSession:
    def __init__(self, api: LaunchAPI, runtime: TrustedRuntime) -> None:
        self.api, self.runtime = api, runtime
        self.identity: ConsoleIdentity | None = None
        self.signal: StopSignal | None = None
        self.job: int | None = None
        self.process: int | None = None
        self.thread: int | None = None
        self.resumed = False
        self.closed = False

    def resume(self) -> None:
        if self.closed or self.runtime.closed:
            raise OSError('session or installed runtime is closed')
        if self.resumed or self.identity is None or self.thread is None:
            raise RuntimeError('session cannot be resumed')
        if self.signal is None or self.signal.is_set():
            raise OSError('session was stopped before resume')
        if (not self.api.is_active(self.identity) or self.api.process_elevated(self.process) or
                not self.alive()):
            raise AuthenticationError('session qualification changed before resume')
        if self.signal.is_set():
            raise OSError('session was stopped during qualification')
        self.api.resume(self.thread)
        self.resumed = True
        self.api.close(self.thread)
        self.thread = None

    def alive(self) -> bool:
        return self.process is not None and self.api.alive(self.process)

    def request_stop(self) -> None:
        if self.closed or self.signal is None:
            raise OSError('session stop signal is closed')
        self.signal.set()

    def close(self) -> None:
        errors = []
        termination_error = None
        if self.process is not None:
            try:
                if self.api.alive(self.process):
                    self.api.terminate(self.process)
            except OSError as error:
                termination_error = error
        # Closing the noninherited job also covers host death and a failed
        # TerminateProcess. A pre-assignment child is terminated above.
        if self.job is not None:
            try:
                self.api.close(self.job)
                self.job = None
            except OSError as error:
                errors.append(error)
        exited = self.process is None
        if self.process is not None:
            try:
                exited = self.api.wait(self.process, 2000)
                if not exited:
                    raise OSError('session process did not exit') from termination_error
            except OSError as error:
                errors.append(error)
        for field in ('thread', 'process') if exited else ('thread',):
            handle = getattr(self, field)
            if handle is not None:
                try:
                    self.api.close(handle)
                    setattr(self, field, None)
                except OSError as error:
                    errors.append(error)
        if self.signal is not None:
            try:
                self.signal.close()
                self.signal = None
            except OSError as error:
                errors.append(error)
        self.closed = self.process is None and self.thread is None and self.job is None and self.signal is None
        if errors:
            raise OSError('session cleanup is incomplete') from errors[0]


class NativeSessionLauncher:
    def __init__(self, runtime: TrustedRuntime, *, api: LaunchAPI | None = None,
                 signal_factory: Callable[[str], StopSignal] | None = None) -> None:
        self.runtime = runtime
        self.api = api if api is not None else NativeLaunchAPI()
        self.api.require_system()
        self.signal_factory = signal_factory or (lambda sid: NativeStopSignal.create(sid))
        self._child: ManagedSession | None = None
        self._lock = threading.Lock()
        self.closed = False
        self._poisoned = False

    @property
    def pending_child(self) -> ManagedSession | None:
        """Only inspect after the serialized create Future has completed."""
        with self._lock:
            return self._child if self._child is not None and not self._child.closed else None

    def _close_temporary(self, handle: int) -> None:
        try:
            self.api.close(handle)
        except OSError:
            self._poisoned = True
            raise

    def _destroy_environment(self, block: int) -> None:
        try:
            self.api.destroy_environment(block)
        except OSError:
            self._poisoned = True
            raise

    def probe(self) -> ConsoleUser | None:
        with self._lock:
            if self.closed or self._poisoned:
                raise OSError('session launcher is closed')
            token, user = self.api.console_token()
            try:
                return user
            finally:
                if token is not None:
                    self._close_temporary(token)

    def create(self, expected: ConsoleUser) -> ManagedSession:
        with self._lock:
            if (self.closed or self._poisoned or self.runtime.closed or
                    self._child is not None and not self._child.closed):
                raise OSError('session launcher is closed or still owns a child')
            if not isinstance(expected, ConsoleUser) or not expected.unlocked:
                raise ValueError('an unlocked console identity is required')
            child = self._child = ManagedSession(self.api, self.runtime)
            try:
                with ExitStack() as temporary:
                    token, current = self.api.console_token()
                    if token is not None:
                        temporary.callback(self._close_temporary, token)
                    if token is None or current != expected:
                        raise AuthenticationError('console changed before session creation')
                    primary = self.api.duplicate(token)
                    temporary.callback(self._close_temporary, primary)
                    environment = self.api.environment(primary)
                    temporary.callback(self._destroy_environment, environment)
                    journal = self.api.journal(primary)
                    child.signal = self.signal_factory(expected.sid)
                    arguments = self.runtime.agent_arguments(journal, stop_event=child.signal.name)
                    child.job = self.api.job()
                    child.process, child.thread, pid = self.api.start(primary, arguments, environment, self.runtime.root)
                    self.api.assign(child.job, child.process)
                    peer = self.api.identity(pid)
                    if (not isinstance(peer, ConsoleIdentity) or
                            (peer.sid, peer.logon_id, peer.session_id) != expected.key or
                            self.api.process_elevated(child.process) or not self.api.is_active(peer) or
                            not child.alive()):
                        raise AuthenticationError('created session process identity is not qualified')
                    child.identity = peer
                return child
            except BaseException:
                child.close()
                raise

    def close(self) -> None:
        with self._lock:
            self.closed = True
            if self._child is not None:
                self._child.close()
