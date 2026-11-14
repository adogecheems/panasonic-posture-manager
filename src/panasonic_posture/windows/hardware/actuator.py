"""LocalSystem-only input execution through the bounded kernel control API.

No device path or packet operation is accepted from a caller. The driver
owns device enrollment, idle admission and the independent three-second
watchdog. A write is never execution evidence without its matching receipt.
This port is not loaded by the ordinary user interface.
"""
from dataclasses import dataclass, field
from collections.abc import Callable
import ctypes as C
import math
import secrets
import struct
import sys
import threading
import time
from typing import Protocol

from panasonic_posture.contracts import ErrorCode
from panasonic_posture.model import Target
from panasonic_posture.service.ports import ActuatorError

REQUEST = struct.Struct('<IIIIQQQ')
REPLY = struct.Struct('<IIIIQQQIIII')
LEASE_MS = 3000
QUERY, ACQUIRE, RENEW, RELEASE = 1, 2, 3, 4
DEVICE = r'\\?\GLOBALROOT\Device\PanasonicPostureControl'
IOCTL = 0x0022e000


class ControlTransport(Protocol):
    def exchange(self, packet: bytes) -> bytes: ...
    def close(self) -> None: ...


class NativeControl:
    def __init__(self) -> None:
        if sys.platform != 'win32':
            raise OSError('input control requires Windows')
        from panasonic_posture.windows.common.security import WindowsSecurity, HANDLE, U32, I32
        security = WindowsSecurity()
        security.require_system()
        self.kernel = security.kernel
        security._fn(self.kernel, 'CreateFileW', HANDLE,
                     C.c_wchar_p, U32, U32, HANDLE, U32, U32, HANDLE)
        security._fn(self.kernel, 'DeviceIoControl', I32,
                     HANDLE, U32, C.c_void_p, U32, C.c_void_p, U32, C.POINTER(U32), HANDLE)
        self.handle = self.kernel.CreateFileW(DEVICE, 0xc0000000, 0, None, 3, 0, None)
        if self.handle == C.c_void_p(-1).value:
            self.handle = None
            raise C.WinError(C.get_last_error())

    def exchange(self, packet: bytes) -> bytes:
        if self.handle is None or not isinstance(packet, bytes) or len(packet) != REQUEST.size:
            raise OSError('invalid or closed control connection')
        request, response, length = C.create_string_buffer(packet), C.create_string_buffer(REPLY.size), C.c_uint32()
        if not self.kernel.DeviceIoControl(self.handle, IOCTL, request, len(packet), response,
                                           REPLY.size, C.byref(length), None):
            raise C.WinError(C.get_last_error())
        if length.value != REPLY.size:
            raise OSError('invalid driver receipt length')
        return response.raw

    def close(self) -> None:
        if self.handle is not None:
            if not self.kernel.CloseHandle(self.handle):
                raise C.WinError(C.get_last_error())
            self.handle = None


@dataclass(frozen=True)
class InputReceipt:
    blocked: bool = False
    started_at: float | None = None
    deadline: float | None = None
    ready: int = 3
    error: str | None = 'input-release-unverified'
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def fresh(self) -> bool:
        if not self.blocked:
            return True
        now = self.clock()
        return self.started_at is not None and self.deadline is not None and self.started_at <= now < self.deadline

    def satisfies(self, target: Target) -> bool:
        if self.error is not None or not self.fresh():
            return False
        return (self.blocked and self.ready == 3) if target.disable_internal_input else not self.blocked


class WindowsActuator:
    touch_activity = None

    def __init__(self, *, factory: Callable[[], ControlTransport] = NativeControl,
                 clock: Callable[[], float] = time.monotonic,
                 nonce_factory: Callable[[], tuple[int, int]] | None = None) -> None:
        self.factory, self.clock = factory, clock
        self.nonce_factory = nonce_factory or (lambda: (secrets.randbits(64) or 1, secrets.randbits(64) or 1))
        self._connection: ControlTransport | None = None
        self._serial = 0
        self._nonce: tuple[int, int] | None = None
        self._receipt = InputReceipt(clock=clock)
        self._verified_release = False
        self._error_code = self._error_detail = None
        self._renew_at = 0.
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self._connection is not None

    @property
    def state(self) -> InputReceipt:
        with self._lock:
            receipt = self._receipt
            if not receipt.fresh():
                return InputReceipt(error='input-lease-expired', clock=self.clock)
            return receipt

    @property
    def error_code(self) -> str | None:
        with self._lock:
            return self._error_code or (ErrorCode.RELAY_HEARTBEAT if not self._receipt.fresh() else None)

    @property
    def error_detail(self) -> str | None:
        return self._error_detail

    def _open(self) -> None:
        if self._connection is not None:
            return
        self._connection = self.factory()
        self._serial, self._nonce = 0, self.nonce_factory()
        if (not isinstance(self._nonce, tuple) or len(self._nonce) != 2 or
                any(type(value) is not int or not 0 <= value < 2**64 for value in self._nonce) or
                not any(self._nonce)):
            raise ValueError('invalid control nonce')

    def _command(self, operation: int) -> InputReceipt:
        if self._connection is None or self._nonce is None:
            raise ValueError('control connection is not open')
        sent = self.clock()
        if type(sent) not in (int, float) or not math.isfinite(sent) or sent < 0:
            raise ValueError('invalid control clock')
        if self._serial >= 2**64 - 1:
            raise ValueError('control sequence exhausted')
        self._serial += 1
        raw = self._connection.exchange(REQUEST.pack(1, REQUEST.size, operation, 0, self._serial, *self._nonce))
        if not isinstance(raw, bytes) or len(raw) != REPLY.size:
            raise ValueError('invalid driver receipt length')
        version, size, status, flags, serial, low, high, ready, remaining, busy, reserved = REPLY.unpack(raw)
        if (version != 1 or size != REPLY.size or status not in range(5) or flags not in (0, 1) or
                serial != self._serial or (low, high) != self._nonce or ready & ~3 or busy & ~7 or
                not 0 <= remaining <= LEASE_MS or reserved or
                bool(flags) != bool(remaining) or flags and ready != 3 or
                status and flags or status == 3 and (not busy or ready != 3)):
            raise ValueError('invalid driver execution receipt')
        if status not in (0, 3) or status == 3 and operation != ACQUIRE:
            raise ValueError(f'driver-status-{status}')
        if (status == 0 and operation in (ACQUIRE, RENEW) and not flags or
                operation == RELEASE and flags):
            raise ValueError('driver did not confirm requested operation')
        receipt = InputReceipt(bool(flags), sent, sent + remaining / 1000 if flags else None,
                               ready, None, self.clock)
        if not receipt.fresh():
            raise ValueError('driver receipt arrived after its deadline')
        return receipt

    def _abort(self, error: Exception, code: str = ErrorCode.RELAY_INPUT) -> None:
        self._error_code, self._error_detail = code, str(error)[:200]
        if self._connection is not None:
            try:
                self._connection.close()
                self._connection = None
                self._verified_release = True
            except OSError as close_error:
                self._error_code, self._error_detail = ErrorCode.NATIVE_RECOVERY, str(close_error)[:200]
        self._receipt = InputReceipt(error=self._error_detail, clock=self.clock)
        raise ActuatorError(self._error_code, self._error_detail) from error

    def keep(self, now: float) -> None:
        with self._lock:
            if self._error_code is not None:
                raise ActuatorError(self._error_code, self._error_detail)
            if self._receipt.blocked and self._receipt.fresh() and self.clock() < self._renew_at:
                return
            try:
                self._open()
                operation = RENEW if self._receipt.blocked else ACQUIRE
                receipt = self._command(operation)
                self._receipt = receipt
                self._verified_release = not receipt.blocked
                self._renew_at = min(self.clock() + .5, receipt.deadline - .25) if receipt.blocked else 0.
            except (OSError, ValueError, struct.error) as error:
                self._abort(error)

    def release(self) -> None:
        with self._lock:
            try:
                if self._connection is None and not self._verified_release:
                    self._open()
                if self._connection is not None:
                    # Closing the exclusive handle also releases the guard.
                    # Even a rejected command must be followed by that close.
                    try:
                        self._command(RELEASE)
                    finally:
                        self._connection.close()
                        self._connection = None
                        self._verified_release = True
                self._receipt = InputReceipt(error=self._error_detail, clock=self.clock)
            except (OSError, ValueError, struct.error) as error:
                self._abort(error, ErrorCode.NATIVE_RECOVERY)

    def retry(self) -> None:
        with self._lock:
            self.release()
            self._error_code = self._error_detail = None
            self._receipt = InputReceipt(error=None, clock=self.clock)

    def follow_hardware(self, now: float) -> None:
        self.release()

    def request_wake(self, serial: int) -> None:
        raise ValueError('Windows touch-backed wake is not implemented')
