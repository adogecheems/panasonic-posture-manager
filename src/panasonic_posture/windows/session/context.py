"""Read-only local-session and display-power observation for the native agent.

WTS work uses one bounded worker. Power callbacks only update guarded state;
they never call the runtime, write display settings or renew a service lease.
Unknown, old and pre-suspend observations cannot establish readiness.
"""
from collections.abc import Callable
from concurrent.futures import Executor, Future
import ctypes as C
from dataclasses import asdict, dataclass
import os
import sys
import threading
import time
from typing import Protocol
from uuid import UUID

from panasonic_posture.tasks import thread_executor
from panasonic_posture.windows.common.security import ExtendedInfo, U32, I32, HANDLE

# Presence freshness for the display qualification gate.  A tighter bound
# shrinks the transient-blip window while the WTS worker stays throttled.
QUERY_INTERVAL = .2
PRESENCE_MAX_AGE = 1.
POWER_SUBSCRIBE_RETRY_SECONDS = 1.
SESSION_DISPLAY_GUID = UUID('2b84c20e-ad23-4ddf-93db-05ffbd7efca5').bytes_le
Callback = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(U32, HANDLE, U32, HANDLE)
PowerCallback = Callable[[str, int | None], None]


class SubscribeParameters(C.Structure):
    _fields_ = [('callback', Callback), ('context', HANDLE)]


class PowerHeader(C.Structure):
    _fields_ = [('guid', C.c_ubyte * 16), ('length', U32)]


class DisplayPowerSetting(C.Structure):
    _fields_ = [('header', PowerHeader), ('value', U32)]


def _bind(dll: object, name: str, result: object, *arguments: object) -> None:
    function = getattr(dll, name)
    function.restype, function.argtypes = result, arguments


@dataclass(frozen=True)
class SessionPresence:
    session_id: int
    console_session_id: int
    active: bool
    locked: bool

    @property
    def attended(self) -> bool:
        return self.control_available and self.locked is False

    @property
    def control_available(self) -> bool:
        return (type(self.session_id) is int and 0 < self.session_id < 0xffffffff and
                type(self.console_session_id) is int and self.session_id == self.console_session_id and
                self.active is True and type(self.locked) is bool)


class NativeSessionPresence:
    """Query this process's session; no SYSTEM token or other user's token."""

    def __init__(self) -> None:
        if sys.platform != 'win32':
            raise OSError('Windows session observation requires Windows')
        self.kernel = C.WinDLL('kernel32', use_last_error=True, winmode=0x800)
        self.wts = C.WinDLL('wtsapi32', use_last_error=True, winmode=0x800)
        _bind(self.kernel, 'ProcessIdToSessionId', I32, U32, C.POINTER(U32))
        _bind(self.kernel, 'WTSGetActiveConsoleSessionId', U32)
        _bind(self.wts, 'WTSQuerySessionInformationW', I32, HANDLE, U32, U32,
              C.POINTER(HANDLE), C.POINTER(U32))
        _bind(self.wts, 'WTSFreeMemory', None, HANDLE)
        session = U32()
        if not self.kernel.ProcessIdToSessionId(os.getpid(), C.byref(session)) or session.value in (0, 0xffffffff):
            raise OSError('native agent requires an interactive user session')
        self.session_id = session.value

    def __call__(self) -> SessionPresence:
        console = self.kernel.WTSGetActiveConsoleSessionId()
        buffer, size = HANDLE(), U32()
        try:
            if not self.wts.WTSQuerySessionInformationW(None, self.session_id, 25,
                                                       C.byref(buffer), C.byref(size)):
                raise OSError(C.get_last_error(), 'session observation failed')
            if not buffer or size.value < C.sizeof(ExtendedInfo):
                raise OSError('incomplete session observation')
            info = C.cast(buffer, C.POINTER(ExtendedInfo)).contents
            if info.level != 1 or info.data.session != self.session_id or info.data.flags not in (0, 1):
                raise OSError('unknown session observation')
            # A console switch during the query cannot qualify this result.
            current = self.kernel.WTSGetActiveConsoleSessionId()
            return SessionPresence(self.session_id, current,
                                   console == current and info.data.state == 0,
                                   info.data.flags == 0)
        finally:
            if buffer:
                self.wts.WTSFreeMemory(buffer)


class PowerSource(Protocol):
    def close(self) -> None: ...


class NativePowerSource:
    """Observe the current session display and suspend/resume, without HWNDs."""

    # A failed unregister must never leave Windows calling a collected ctypes
    # trampoline. Roots are removed only after all native registrations close.
    _live: dict[int, 'NativePowerSource'] = {}

    def __init__(self, callback: PowerCallback, *, api=None) -> None:
        if api is None:
            if sys.platform != 'win32':
                raise OSError('Windows power observation requires Windows')
            api = C.WinDLL('powrprof', use_last_error=True, winmode=0x800)
        self.api, self.notify = api, callback
        self.closed = False
        self.error: str | None = None
        self.handles: list[tuple[int, str]] = []
        self.callback = Callback(self._callback)
        self.parameters = SubscribeParameters(self.callback, None)
        self.guid = (C.c_ubyte * 16).from_buffer_copy(SESSION_DISPLAY_GUID)
        _bind(api, 'PowerSettingRegisterNotification', U32, HANDLE, U32, HANDLE, C.POINTER(HANDLE))
        _bind(api, 'PowerRegisterSuspendResumeNotification', U32, U32, HANDLE, C.POINTER(HANDLE))
        _bind(api, 'PowerSettingUnregisterNotification', U32, HANDLE)
        _bind(api, 'PowerUnregisterSuspendResumeNotification', U32, HANDLE)
        self._live[id(self)] = self
        try:
            handle = HANDLE()
            code = api.PowerSettingRegisterNotification(C.byref(self.guid), 2,
                                                        C.byref(self.parameters), C.byref(handle))
            if code or not handle.value:
                raise OSError(code, 'display-power registration failed')
            self.handles.append((handle.value, 'PowerSettingUnregisterNotification'))
            handle = HANDLE()
            code = api.PowerRegisterSuspendResumeNotification(2, C.byref(self.parameters), C.byref(handle))
            if code or not handle.value:
                raise OSError(code, 'suspend registration failed')
            self.handles.append((handle.value, 'PowerUnregisterSuspendResumeNotification'))
        except BaseException:
            self.close()
            raise

    def _callback(self, context: int | None, kind: int, setting: int | None) -> int:
        if self.closed:
            return 0
        try:
            if kind == 0x8013:
                if not setting:
                    raise ValueError('missing power setting')
                header = C.cast(setting, C.POINTER(PowerHeader)).contents
                if bytes(header.guid) != SESSION_DISPLAY_GUID:
                    return 0
                if header.length != 4:
                    raise ValueError('invalid display-power size')
                value = C.cast(setting, C.POINTER(DisplayPowerSetting)).contents.value
                if value not in (0, 1, 2):
                    raise ValueError('unknown display-power state')
                self.notify('display', value)
            elif kind == 4:
                self.notify('suspend', None)
            elif kind in (7, 0x12):
                self.notify('resume', None)
            else:
                return 0
            self.error = None
            return 0
        except Exception as error:
            # Exceptions cannot escape a ctypes callback. Report unknown state
            # through the same bounded observer, never invent an on value.
            self.error = f'invalid power callback: {type(error).__name__}'
            try:
                self.notify('error', None)
            except Exception as failed:
                self.error += f'; error observer failed: {type(failed).__name__}'
            return 13  # ERROR_INVALID_DATA

    def close(self) -> None:
        self.closed = True
        failed = []
        for handle, method in reversed(self.handles):
            code = getattr(self.api, method)(handle)
            if code:
                failed.append((handle, method))
        self.handles = failed
        if failed:
            raise OSError('power notification cleanup failed')
        self._live.pop(id(self), None)


class WindowsSessionContext:
    def __init__(self, *, query: Callable[[], SessionPresence] | None = None,
                 power_factory: Callable[[PowerCallback], PowerSource] = NativePowerSource,
                 clock: Callable[[], float] = time.monotonic, executor: Executor | None = None) -> None:
        self.query = query if query is not None else NativeSessionPresence()
        self.power_factory, self.clock = power_factory, clock
        self._executor = executor if executor is not None else thread_executor('windows-session-context')
        self._owned_executor = executor is None
        self._owner = threading.get_ident()
        self._lock = threading.Lock()
        self._closed = self._fatal = self._suspended = self._rearm = False
        self._resume_seen = False
        self._presence: SessionPresence | None = None
        self._display: bool | None = None
        self._checked = float('-inf')
        self._revision = self._subscription = 0
        self._power_retry_at = 0.
        self._pending: tuple[Future, float, int] | None = None
        self._poll_at = 0.
        self._power: PowerSource | None = None
        self.error: str | None = None
        try:
            self._subscribe()
        except BaseException:
            self.close()
            raise

    def _check_owner(self) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError('session context polling requires its owner thread')

    def _subscribe(self) -> None:
        with self._lock:
            self._subscription += 1
            subscription = self._subscription
            self._display = None
            self._rearm = False
        old, self._power = self._power, None
        try:
            if old is not None:
                old.close()
            self._power = self.power_factory(lambda kind, value: self._event(subscription, kind, value))
            with self._lock:
                self._power_retry_at = self.clock() + POWER_SUBSCRIBE_RETRY_SECONDS
        except (OSError, ValueError):
            with self._lock:
                self._fatal = True
                self.error = 'power observer unavailable'
            raise

    def _event(self, subscription: int, kind: str, value: int | None) -> None:
        with self._lock:
            if self._closed or self._fatal or subscription != self._subscription:
                return
            # Windows can send automatic resume followed by user resume. Keep
            # this fence across re-subscriptions so the latter cannot undo an
            # already recovered display transaction. A new suspend rearms it.
            if kind == 'resume' and self._resume_seen:
                return
            if kind == 'display':
                self._display = value != 0 if type(value) is int and value in (0, 1, 2) else None
                if self._display is not None:
                    self._power_retry_at = 0.
            else:
                self._presence = None
                self._revision += 1
                self._display = None
                if kind == 'suspend':
                    self._suspended = True
                    self._resume_seen = False
                elif kind == 'resume':
                    self._suspended = False
                    self._rearm = True
                    self._resume_seen = True

    def unattended(self) -> bool:
        with self._lock:
            return self._unattended_at(self.clock())

    def _unattended_at(self, now: float) -> bool:
        return (self._unavailable_at(now) or self._display is False or
                self._presence is None or not self._presence.attended)

    def control_available(self) -> bool:
        """A known off display is context, not lost session qualification."""
        with self._lock:
            return not self._unavailable_at(self.clock())

    def _unavailable_at(self, now: float) -> bool:
        return (self._closed or self._fatal or self._suspended or self._display is None or
                getattr(self._power, 'error', None) is not None or
                self._presence is None or not self._presence.control_available or
                not 0 <= now - self._checked <= PRESENCE_MAX_AGE)

    def snapshot(self) -> dict:
        """Copy coarse diagnostic flags, never identity strings or input data."""
        with self._lock:
            now = self.clock()
            return dict(unattended=self._unattended_at(now), display_on=self._display,
                        control_available=not self._unavailable_at(now),
                        presence_stale=(self._presence is not None
                                        and now - self._checked > PRESENCE_MAX_AGE),
                        suspended=self._suspended, error=self.error or getattr(self._power, 'error', None),
                        presence=asdict(self._presence) if self._presence is not None else None)

    def poll(self) -> None:
        self._check_owner()
        with self._lock:
            if self._closed or self._fatal:
                return
            rearm = self._rearm
        if rearm:
            self._subscribe()
            self._poll_at = 0.
        now = self.clock()
        self._collect(now)
        with self._lock:
            retry_power = (
                not self._closed
                and not self._fatal
                and not self._suspended
                and self._display is None
                and now >= self._power_retry_at
            )
        if retry_power:
            # UAC and other secure-desktop transitions can consume the initial
            # display callback. Re-registering on the owner thread recovers
            # once the normal desktop is active again without guessing the
            # display state or restarting the session process.
            self._subscribe()
            self._poll_at = 0.
        if self._pending is not None:
            return  # One hung WTS query cannot create more workers/jobs.
        if now >= self._poll_at:
            with self._lock:
                if self._suspended:
                    return
                revision = self._revision
            self._poll_at = now + QUERY_INTERVAL
            self._pending = self._executor.submit(self.query), now, revision
            self._collect(self.clock())

    def _collect(self, now: float) -> None:
        if self._pending is None or not self._pending[0].done():
            return
        future, started, revision = self._pending
        self._pending = None
        try:
            result = future.result()
            if not isinstance(result, SessionPresence):
                raise ValueError('invalid session presence')
            with self._lock:
                if revision == self._revision and 0 <= now - started <= PRESENCE_MAX_AGE:
                    self._presence, self._checked = result, started
                    self.error = None
        except (OSError, ValueError) as error:
            with self._lock:
                self._presence = None
                self.error = str(error)[:200]

    def close(self) -> None:
        self._check_owner()
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._presence = None
        try:
            if self._power is not None:
                self._power.close()
        finally:
            if self._owned_executor:
                self._executor.shutdown(wait=False, cancel_futures=True)
