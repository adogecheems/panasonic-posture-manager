"""LocalSystem-owned, local-only message pipes for the hardware host.

This transport is opt-in composition for an installed hardware service, not
used by the ordinary preview. It does not install services, load a driver or
open an input device. The host loop never waits for token/WTS authentication.
"""
import ctypes as C
from collections.abc import Callable
from dataclasses import dataclass
import json
import time

from ..contracts import ALL_ERROR_CODES
from ..model import StateSnapshot
from ..service.engine import Engine, ServiceError
from ..snapshot_codec import parse_snapshot
from ..tasks import thread_executor
from .hardware_host import ConsoleIdentity, HardwareHost, CHANNEL
from .protocol import MAX_FRAME, encode, decode, ProtocolError
from .security import WindowsSecurity, SecurityAttributes, U32, I32, HANDLE, AuthenticationError

PIPE_NAME = r'\\.\pipe\PanasonicPostureManager.Hardware.v1'
# FILE_GENERIC_READ | FILE_GENERIC_WRITE minus FILE_CREATE_PIPE_INSTANCE (4).
# GR/GW in the client ACE would accidentally let clients create server pipes.
CLIENT_ACCESS = 0x12019b
PIPE_SDDL = 'O:SYG:SYD:P(A;;GA;;;SY)(A;;0x12019b;;;IU)'
AUTH_SECONDS = 1.0
MAX_CLIENTS = 4
CLIENT_TIMEOUT = 5.0


class NativePipes:
    def __init__(self, security: WindowsSecurity | None = None) -> None:
        self.security = security or WindowsSecurity()
        self.kernel = self.security.kernel
        p = C.POINTER
        fn = self.security._fn
        fn(self.kernel, 'CreateNamedPipeW', HANDLE, C.c_wchar_p, U32, U32, U32, U32, U32, U32, p(SecurityAttributes))
        fn(self.kernel, 'ConnectNamedPipe', I32, HANDLE, HANDLE)
        fn(self.kernel, 'DisconnectNamedPipe', I32, HANDLE)
        fn(self.kernel, 'CreateFileW', HANDLE, C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE)
        fn(self.kernel, 'SetNamedPipeHandleState', I32, HANDLE, p(U32), HANDLE, HANDLE)
        fn(self.kernel, 'ReadFile', I32, HANDLE, HANDLE, U32, p(U32), HANDLE)
        fn(self.kernel, 'WriteFile', I32, HANDLE, HANDLE, U32, p(U32), HANDLE)

    @staticmethod
    def valid_name(name: str) -> None:
        if not isinstance(name, str) or not name.startswith('\\\\.\\pipe\\') or not 9 < len(name) <= 180 or any(
                char not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for char in name[9:]):
            raise ValueError('a local pipe name is required')

    def create(self, name: str, *, first: bool) -> int:
        self.valid_name(name)
        self.security.require_system()
        descriptor = HANDLE()
        self.security._check(self.security.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            PIPE_SDDL, 1, C.byref(descriptor), None))
        try:
            attrs = SecurityAttributes(C.sizeof(SecurityAttributes), descriptor, False)
            handle = self.kernel.CreateNamedPipeW(name, 3 | (0x80000 if first else 0),
                4 | 2 | 1 | 8, MAX_CLIENTS, MAX_FRAME, MAX_FRAME, 0, C.byref(attrs))
            if handle == C.c_void_p(-1).value:
                raise C.WinError(C.get_last_error())
            return handle
        finally:
            self.kernel.LocalFree(descriptor)

    def connect(self, name: str) -> int:
        self.valid_name(name)
        # Identification only: the server may read this token, never use the
        # client's credentials to access other resources on its behalf.
        handle = self.kernel.CreateFileW(name, CLIENT_ACCESS, 0, None, 3, 0x100000 | 0x10000, None)
        if handle == C.c_void_p(-1).value:
            raise C.WinError(C.get_last_error())
        try:
            self.security.require_system_pipe(handle)
            mode = U32(2 | 1)
            self.security._check(self.kernel.SetNamedPipeHandleState(handle, C.byref(mode), None, None))
            return handle
        except BaseException:
            self.close(handle)
            raise

    def accept(self, handle: int) -> bool:
        if self.kernel.ConnectNamedPipe(handle, None):
            return True
        error = C.get_last_error()
        if error == 535:  # ERROR_PIPE_CONNECTED
            return True
        if error in (536, 232):  # LISTENING / NO_DATA
            return False
        raise OSError(error, 'hardware pipe connection failed')

    def read(self, handle: int) -> dict | None:
        data, size = C.create_string_buffer(MAX_FRAME), U32()
        if not self.kernel.ReadFile(handle, data, MAX_FRAME, C.byref(size), None):
            error = C.get_last_error()
            if error == 232:  # empty nonblocking pipe
                return None
            if error == 234:
                raise ProtocolError('hardware frame exceeds the message limit')
            raise OSError(error, 'hardware pipe disconnected')
        if not size.value:
            raise ProtocolError('empty hardware message')
        return decode(data.raw[:size.value])

    def write(self, handle: int, value: dict) -> None:
        frame, size = encode(value), U32()
        if not self.kernel.WriteFile(handle, frame, len(frame), C.byref(size), None) or size.value != len(frame):
            raise OSError(C.get_last_error(), 'hardware pipe write failed or client stalled')

    def reset(self, handle: int) -> None:
        self.kernel.DisconnectNamedPipe(handle)

    def close(self, handle: int) -> None:
        self.kernel.CloseHandle(handle)


class ConsoleGuard:
    """Fresh cached decisions; slow session validation cannot renew a lease."""

    def __init__(self, check: Callable[[ConsoleIdentity], bool], agent: ConsoleIdentity | None, *,
                 clock: Callable[[], float] = time.monotonic, executor=None) -> None:
        self.check, self.agent, self.clock = check, agent, clock
        self.executor = executor or thread_executor('windows-session-auth')
        self.owned_executor = executor is None
        self.entries = {}
        self.pending = None
        self.next_check = 0.

    def remember(self, peer: ConsoleIdentity, checked_at: float, valid: bool = True) -> None:
        self.entries[peer] = checked_at, valid

    def allow(self, peer: ConsoleIdentity) -> bool:
        checked, valid = self.entries.get(peer, (float('-inf'), False))
        return valid is True and 0 <= self.clock() - checked < AUTH_SECONDS

    def forget(self, peer: ConsoleIdentity) -> None:
        if peer != self.agent:
            self.entries.pop(peer, None)

    def tick(self) -> None:
        if self.pending is not None and self.pending.done():
            try:
                checked, valid = self.pending.result()
                self.remember(self.agent, checked, valid)
            except OSError:
                self.entries.pop(self.agent, None)
            self.pending = None
        if self.agent is not None and self.pending is None and self.clock() >= self.next_check:
            def check():
                checked = self.clock()
                return checked, self.check(self.agent)
            self.pending = self.executor.submit(check)
            self.next_check = self.clock() + .5

    def close(self) -> None:
        self.entries.clear()
        if self.owned_executor:
            self.executor.shutdown(wait=False, cancel_futures=True)


@dataclass
class Slot:
    handle: object
    connected: bool = False
    peer: object = None
    serial: int = 0
    touched: float = 0.
    pending: object = None
    request: object = None
    retired: bool = False


class HardwareServer:
    def __init__(self, engine: Engine, *, session_process: ConsoleIdentity | None = None,
                 security: WindowsSecurity | None = None, pipes: NativePipes | None = None,
                 pipe_name: str = PIPE_NAME, clock: Callable[[], float] = time.monotonic,
                 executor=None, guard_executor=None) -> None:
        self.security = security or WindowsSecurity()
        self.security.require_system()
        self.pipes = pipes or NativePipes(self.security)
        self.clock = clock
        self.guard = ConsoleGuard(self.security.is_active, session_process,
                                  clock=clock, executor=guard_executor)
        self.host = HardwareHost(engine, authorize=self.guard.allow, session_process=session_process)
        self.executor = executor or thread_executor('windows-request-auth')
        self.owned_executor = executor is None
        self.slots = []
        self.closed = False
        try:
            for index in range(MAX_CLIENTS):
                self.slots.append(Slot(self.pipes.create(pipe_name, first=index == 0)))
        except BaseException:
            self.close()
            raise

    def _reset(self, slot: Slot) -> None:
        if slot.peer is not None:
            self.host.disconnected(slot.peer)
            self.guard.forget(slot.peer)
        slot.peer = None
        # Keep the handle while an authentication worker uses it. At most four
        # slots/jobs exist, including timed-out requests, even if WTS hangs.
        if slot.pending is not None and not slot.pending.done():
            slot.retired = True
            return
        self.pipes.reset(slot.handle)
        slot.connected, slot.serial, slot.pending = False, 0, None
        slot.request, slot.retired = None, False

    def poll(self) -> None:
        if self.closed:
            return
        self.guard.tick()
        self.host.tick()
        for slot in self.slots:
            try:
                if slot.retired:
                    if slot.pending.done():
                        self._reset(slot)
                    continue
                if not slot.connected:
                    if not self.pipes.accept(slot.handle):
                        continue
                    slot.connected, slot.touched = True, self.clock()
                if self.clock() - slot.touched >= CLIENT_TIMEOUT:
                    self._reset(slot)
                    continue
                if slot.pending is not None:
                    if not slot.pending.done():
                        continue
                    peer, checked = slot.pending.result()
                    slot.pending = None
                    if slot.peer is not None and slot.peer != peer:
                        raise AuthenticationError('pipe client identity changed')
                    slot.peer = peer
                    self.guard.remember(peer, checked)
                    request = slot.request
                    if type(request.get('id')) is not int or request['id'] <= slot.serial:
                        raise ProtocolError('replayed hardware request')
                    slot.serial = request['id']
                    try:
                        reply = self.host.dispatch(peer, request)
                        reply['error'] = None
                    except ServiceError as error:
                        reply = dict(version=1, channel=CHANNEL, type='response', id=request['id'],
                            instance=self.host.instance, accepted=False, error=error.code,
                            state=self.host.engine.state.to_dict())
                    self.pipes.write(slot.handle, reply)
                    slot.request = None
                    slot.touched = self.clock()
                    continue
                request = self.pipes.read(slot.handle)
                if request is not None:
                    slot.request = request
                    handle = slot.handle
                    def authenticate(handle=handle):
                        checked = self.clock()
                        return self.security.peer(handle), checked
                    slot.pending = self.executor.submit(authenticate)
            except (OSError, ValueError):
                self._reset(slot)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.host.close()
        finally:
            for slot in self.slots:
                self.pipes.close(slot.handle)
            self.slots.clear()
            self.guard.close()
            if self.owned_executor:
                self.executor.shutdown(wait=False, cancel_futures=True)


class HardwareConnection:
    """Blocking request client for a worker, never the GUI/main-loop thread."""

    def __init__(self, *, pipe_name: str = PIPE_NAME, pipes: NativePipes | None = None,
                 timeout: float = 5., clock: Callable[[], float] = time.monotonic) -> None:
        self.pipes = pipes or NativePipes()
        self.handle = self.pipes.connect(pipe_name)
        self.timeout, self.clock = timeout, clock
        self.instance, self.serial = None, 0

    def request(self, method: str, params: dict) -> dict:
        if self.handle is None:
            raise OSError('hardware connection is closed')
        self.serial += 1
        try:
            self.pipes.write(self.handle, dict(version=1, channel=CHANNEL, id=self.serial,
                instance=self.instance, method=method, params=params))
            deadline = self.clock() + self.timeout
            while self.clock() < deadline:
                reply = self.pipes.read(self.handle)
                if reply is None:
                    time.sleep(.01)
                    continue
                if (reply.get('channel') != CHANNEL or reply.get('type') != 'response' or
                        type(reply.get('id')) is not int or reply['id'] != self.serial or
                        type(reply.get('accepted')) is not bool or 'error' not in reply or
                        reply['error'] is not None and (not isinstance(reply['error'], str) or
                                                       reply['error'] not in ALL_ERROR_CODES)):
                    raise ProtocolError('invalid hardware response')
                value = reply.get('state')
                if (not isinstance(value, dict) or not set(StateSnapshot().to_dict()) <= value.keys() or
                        type(value.get('version')) is not int):
                    raise ProtocolError('incomplete hardware state')
                state = parse_snapshot(json.dumps(value, allow_nan=False))
                if not state.instance or state.instance != reply.get('instance') or self.instance not in (None, state.instance):
                    raise ProtocolError('hardware instance changed')
                self.instance = state.instance
                if reply['error'] is not None:
                    raise ServiceError(reply['error'])
                return reply
            raise TimeoutError('hardware request timed out')
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self.handle is not None:
            self.pipes.close(self.handle)
            self.handle = None
