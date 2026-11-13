"""Read physical convertible-slate notifications through Windows ETW.

The existing Intel HID Event Filter reports ACPI notifications without
requiring another kernel driver.  Event 115 is distinct from the indicator
status event used for software CSM writes.  This module deliberately exposes
only a read-only physical source; it never calls the ACPI control method or
the shell tablet-mode setter.
"""
from __future__ import annotations

import ctypes as C
from collections.abc import Callable
from dataclasses import dataclass
import logging
import struct
import sys
import threading
import uuid

from ..model import HardwareStatus

log = logging.getLogger(__name__)

HID_EVENT_FILTER_GUID = 'dde50426-fa77-4088-8e0c-f2f553fb6843'
ACPI_NOTIFY_EVENT_ID = 115
ACPI_TABLET_NOTIFY = 0xCC
ACPI_LAPTOP_NOTIFY = 0xCD

_EVENT_TRACE_REAL_TIME_MODE = 0x00000100
_PROCESS_TRACE_MODE_EVENT_RECORD = 0x10000000
_INVALID_TRACE_HANDLE = C.c_uint64(-1).value
_ERROR_ACCESS_DENIED = 5
_ERROR_ALREADY_EXISTS = 183
_TRACE_LEVEL_INFORMATION = 4
_EVENT_CONTROL_CODE_ENABLE_PROVIDER = 1
_EVENT_TRACE_CONTROL_STOP = 1

# Layouts are the 64-bit Windows 10/11 ABI.  The values are asserted by
# tests so an incompatible runtime cannot silently misinterpret ETW memory.
_EVENT_TRACE_PROPERTIES_SIZE = 120
_EVENT_TRACE_LOGFILE_SIZE = 448
_TRACE_LOGFILE_PROCESS_MODE_OFFSET = 28
_TRACE_LOGFILE_EVENT_CALLBACK_OFFSET = 424


class _Guid(C.Structure):
    _fields_ = (
        ('Data1', C.c_uint32),
        ('Data2', C.c_uint16),
        ('Data3', C.c_uint16),
        ('Data4', C.c_ubyte * 8),
    )

    @classmethod
    def parse(cls, value: str) -> '_Guid':
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class _EventDescriptor(C.Structure):
    _fields_ = (
        ('Id', C.c_uint16),
        ('Version', C.c_ubyte),
        ('Channel', C.c_ubyte),
        ('Level', C.c_ubyte),
        ('Opcode', C.c_ubyte),
        ('Task', C.c_uint16),
        ('Keyword', C.c_uint64),
    )


class _EventHeader(C.Structure):
    _fields_ = (
        ('Size', C.c_uint16),
        ('HeaderType', C.c_uint16),
        ('Flags', C.c_uint16),
        ('EventProperty', C.c_uint16),
        ('ThreadId', C.c_uint32),
        ('ProcessId', C.c_uint32),
        ('TimeStamp', C.c_int64),
        ('ProviderId', _Guid),
        ('EventDescriptor', _EventDescriptor),
        ('ProcessorTime', C.c_uint64),
        ('ActivityId', _Guid),
    )


class _EtwBufferContext(C.Structure):
    _fields_ = (('ProcessorNumber', C.c_uint16), ('LoggerId', C.c_uint16))


class _EventRecord(C.Structure):
    _fields_ = (
        ('EventHeader', _EventHeader),
        ('BufferContext', _EtwBufferContext),
        ('ExtendedDataCount', C.c_uint16),
        ('UserDataLength', C.c_uint16),
        ('ExtendedData', C.c_void_p),
        ('UserData', C.c_void_p),
        ('UserContext', C.c_void_p),
    )


_EventRecordCallback = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(
    None, C.POINTER(_EventRecord))


def parse_acpi_notification(data: bytes) -> bool | None:
    """Return physical slate state from one ACPI-notification payload.

    The provider manifest describes two integer fields.  Use only an
    unambiguous transition marker so metadata or unknown provider extensions
    cannot be mistaken for a switch event.
    """
    if not isinstance(data, bytes) or len(data) < 8 or len(data) % 4:
        return None
    words = struct.unpack('<%dI' % (len(data) // 4), data)
    transitions = [word for word in words if word in (ACPI_TABLET_NOTIFY, ACPI_LAPTOP_NOTIFY)]
    if len(transitions) != 1:
        return None
    return transitions[0] == ACPI_TABLET_NOTIFY


class EtwBackend:
    """Small injectable boundary around the real-time ETW consumer."""

    def start(self, callback: Callable[[bytes], None]) -> None: ...
    def close(self) -> None: ...
    @property
    def failure(self) -> BaseException | None: ...


class WindowsHidEventFilterBackend(EtwBackend):
    """Consume the Intel HID Event Filter's ACPI-notification ETW provider.

    ETW control is a privileged operation.  The caller must run this backend
    in the LocalSystem hardware host; ordinary UI processes must not use
    it as an unauthenticated hardware channel.
    """

    def __init__(self, *, session_name: str = 'PanasonicPostureManager.PhysicalTablet.v1',
                 provider_guid: str = HID_EVENT_FILTER_GUID) -> None:
        if sys.platform != 'win32':
            raise OSError('HID Event Filter ETW requires Windows')
        self.session_name = session_name
        self.provider_guid = provider_guid
        self._advapi = C.WinDLL('advapi32', use_last_error=True)
        self._bind()
        self._callback_ref = None
        self._process_thread: threading.Thread | None = None
        self._session_handle = C.c_uint64()
        self._consumer_handle = C.c_uint64()
        self._logfile = (C.c_ubyte * _EVENT_TRACE_LOGFILE_SIZE)()
        self._properties = (C.c_ubyte * (
            _EVENT_TRACE_PROPERTIES_SIZE + (len(session_name) + 1) * 2
        ))()
        self._started = False
        self._failure: BaseException | None = None

    def _bind(self) -> None:
        def bind(name, result, *args):
            function = getattr(self._advapi, name)
            function.restype, function.argtypes = result, args

        bind('StartTraceW', C.c_uint32, C.POINTER(C.c_uint64), C.c_wchar_p, C.c_void_p)
        bind('ControlTraceW', C.c_uint32, C.c_uint64, C.c_wchar_p, C.c_void_p, C.c_uint32)
        bind('EnableTraceEx2', C.c_uint32, C.c_uint64, C.POINTER(_Guid), C.c_uint32,
             C.c_ubyte, C.c_uint64, C.c_uint64, C.c_uint32, C.c_void_p)
        bind('OpenTraceW', C.c_uint64, C.c_void_p)
        bind('ProcessTrace', C.c_uint32, C.POINTER(C.c_uint64), C.c_uint32, C.c_void_p, C.c_void_p)
        bind('CloseTrace', C.c_uint32, C.c_uint64)

    @property
    def failure(self) -> BaseException | None:
        return self._failure

    def _stop_stale_session(self) -> None:
        """Stop only our fixed session name after an interrupted service run."""
        total = len(self._properties)
        self._set_properties_size(total)
        struct.pack_into('<I', self._properties, 116, _EVENT_TRACE_PROPERTIES_SIZE)
        name = self.session_name.encode('utf-16-le') + b'\0\0'
        self._properties[_EVENT_TRACE_PROPERTIES_SIZE:][:len(name)] = name
        self._advapi.ControlTraceW(0, self.session_name, self._properties, _EVENT_TRACE_CONTROL_STOP)

    def _set_properties_size(self, total: int) -> None:
        struct.pack_into('<I', self._properties, 0, total)
        struct.pack_into('<I', self._properties, 48, 64)  # BufferSize
        struct.pack_into('<I', self._properties, 52, 4)   # MinimumBuffers
        struct.pack_into('<I', self._properties, 56, 8)   # MaximumBuffers
        struct.pack_into('<I', self._properties, 64, _EVENT_TRACE_REAL_TIME_MODE)
        struct.pack_into('<I', self._properties, 68, 1)   # FlushTimer
        struct.pack_into('<I', self._properties, 112, 0)  # no log file
        struct.pack_into('<I', self._properties, 116, _EVENT_TRACE_PROPERTIES_SIZE)

    def start(self, callback: Callable[[bytes], None]) -> None:
        if self._started:
            raise OSError('ETW backend is already started')
        self._callback_ref = callback
        total = len(self._properties)
        self._set_properties_size(total)
        name = self.session_name.encode('utf-16-le') + b'\0\0'
        self._properties[_EVENT_TRACE_PROPERTIES_SIZE:][:len(name)] = name
        session = C.c_uint64()
        result = self._advapi.StartTraceW(C.byref(session), self.session_name, self._properties)
        if result == _ERROR_ALREADY_EXISTS:
            self._stop_stale_session()
            self._set_properties_size(total)
            name = self.session_name.encode('utf-16-le') + b'\0\0'
            self._properties[_EVENT_TRACE_PROPERTIES_SIZE:][:len(name)] = name
            result = self._advapi.StartTraceW(C.byref(session), self.session_name, self._properties)
        if result:
            if result == _ERROR_ACCESS_DENIED:
                raise PermissionError(result, 'ETW control requires LocalSystem or an ETW-capable account')
            raise OSError(result, 'cannot start HID Event Filter ETW session')
        self._session_handle = session
        provider = _Guid.parse(self.provider_guid)
        result = self._advapi.EnableTraceEx2(
            self._session_handle.value, C.byref(provider),
            _EVENT_CONTROL_CODE_ENABLE_PROVIDER, _TRACE_LEVEL_INFORMATION,
            1, 0, 0, None,
        )
        if result:
            self._stop()
            raise OSError(result, 'cannot enable HID Event Filter provider')
        self._logfile[:] = b'\0' * len(self._logfile)
        logger_name = C.c_wchar_p(self.session_name)
        C.c_void_p.from_buffer(self._logfile, 8).value = C.cast(logger_name, C.c_void_p).value
        C.c_uint32.from_buffer(self._logfile, _TRACE_LOGFILE_PROCESS_MODE_OFFSET).value = (
            _PROCESS_TRACE_MODE_EVENT_RECORD | _EVENT_TRACE_REAL_TIME_MODE
        )
        callback_ref = _EventRecordCallback(self._on_record)
        C.c_void_p.from_buffer(self._logfile, _TRACE_LOGFILE_EVENT_CALLBACK_OFFSET).value = (
            C.cast(callback_ref, C.c_void_p).value
        )
        trace = self._advapi.OpenTraceW(C.cast(self._logfile, C.c_void_p))
        if trace == _INVALID_TRACE_HANDLE:
            self._stop()
            raise OSError(C.get_last_error(), 'cannot open HID Event Filter ETW consumer')
        self._consumer_handle = C.c_uint64(trace)
        self._callback_native = callback_ref
        self._logger_name = logger_name
        self._started = True
        self._process_thread = threading.Thread(
            target=self._process, name='windows-hid-etw', daemon=True)
        self._process_thread.start()

    def _process(self) -> None:
        handle = C.c_uint64(self._consumer_handle.value)
        try:
            result = self._advapi.ProcessTrace(
                C.byref(handle), 1, None, None)
            if result and self._started:
                self._failure = OSError(result, 'HID Event Filter ETW processing failed')
        except BaseException as error:  # ETW thread must never escape silently.
            self._failure = error

    def _on_record(self, record_pointer) -> None:
        try:
            record = record_pointer.contents
            if record.EventHeader.EventDescriptor.Id != ACPI_NOTIFY_EVENT_ID:
                return
            if not record.UserData or not record.UserDataLength:
                return
            data = C.string_at(record.UserData, record.UserDataLength)
            if self._callback_ref is not None:
                self._callback_ref(data)
        except BaseException as error:
            self._failure = error

    def _stop(self) -> None:
        self._started = False
        if self._session_handle.value:
            self._advapi.ControlTraceW(
                self._session_handle.value, self.session_name, self._properties,
                _EVENT_TRACE_CONTROL_STOP)
        if self._consumer_handle.value:
            self._advapi.CloseTrace(self._consumer_handle.value)
            self._consumer_handle = C.c_uint64()
        self._session_handle = C.c_uint64()

    def close(self) -> None:
        self._stop()
        if self._process_thread is not None:
            self._process_thread.join(timeout=2.0)
            self._process_thread = None


@dataclass(frozen=True)
class _SwitchState:
    value: bool | None
    status: HardwareStatus


class AcpiPhysicalSwitch:
    """Expose only independently observed physical ACPI notifications.

    The convertible-slate indicator (CSM) is a software output and can reflect
    this application's own writes.  It must never seed or replace this state.
    """

    def __init__(
        self,
        backend: EtwBackend | None = None,
        initial_reader: Callable[[], bool | None] | None = None,
        *,
        trusted_startup_baseline: bool = True,
    ) -> None:
        if type(trusted_startup_baseline) is not bool:
            raise ValueError('startup baseline trust must be explicit')
        self.backend = backend or WindowsHidEventFilterBackend()
        self.initial_reader = initial_reader
        self.trusted_startup_baseline = trusted_startup_baseline
        self._lock = threading.Lock()
        self._state = _SwitchState(None, HardwareStatus.ABSENT)
        self._started = self._bootstrap_failed = False

    def start(self) -> None:
        with self._lock:
            if self._started:
                raise OSError('physical switch is already started')
            self._started = True
        try:
            self.backend.start(self._ingest)
        except BaseException as error:
            try:
                self.backend.close()
            except (OSError, ValueError):
                pass
            with self._lock:
                self._started = False
                self._state = _SwitchState(
                    None, HardwareStatus.PERMISSION
                    if isinstance(error, PermissionError) else HardwareStatus.READ_ERROR)
            log.warning('cannot start physical ETW switch: %s', error)

    def bootstrap(self) -> None:
        """Acquire the optional read-only startup baseline after sensor start."""

        if (not self.trusted_startup_baseline or self.initial_reader is None
                or self._bootstrap_failed):
            return
        with self._lock:
            if not self._started or self._state.status != HardwareStatus.ABSENT:
                return
        if self.backend.failure is not None:
            return
        try:
            initial = self.initial_reader()
            if initial is not None and type(initial) is not bool:
                raise ValueError('initial physical state is not boolean')
        except (OSError, ValueError) as error:
            log.warning('cannot bootstrap physical switch state: %s', error)
            self._bootstrap_failed = True
            return
        if initial is None:
            return
        with self._lock:
            # Never overwrite an ETW event that arrived while the read-only
            # startup baseline was being acquired.
            if self._started and self._state.status == HardwareStatus.ABSENT:
                self._state = _SwitchState(initial, HardwareStatus.OK)

    def finalize_bootstrap(self, fallback: bool) -> None:
        """Ensure startup has a boolean baseline even when auxiliary reads fail."""

        if type(fallback) is not bool:
            raise ValueError('fallback physical state must be boolean')
        if not self.trusted_startup_baseline:
            return
        if self.backend.failure is not None:
            return
        with self._lock:
            if self._started and self._state.status == HardwareStatus.ABSENT:
                self._state = _SwitchState(fallback, HardwareStatus.OK)

    def _ingest(self, payload: bytes) -> None:
        value = parse_acpi_notification(payload)
        if value is None:
            return
        with self._lock:
            if self._started:
                self._state = _SwitchState(value, HardwareStatus.OK)

    def query(self) -> tuple[bool | None, HardwareStatus]:
        with self._lock:
            if self.backend.failure is not None:
                self._state = _SwitchState(None, HardwareStatus.READ_ERROR)
            return self._state.value, self._state.status

    def close(self) -> None:
        self.backend.close()
        with self._lock:
            self._started = False
            self._state = _SwitchState(None, HardwareStatus.ABSENT)


__all__ = [
    'ACPI_LAPTOP_NOTIFY', 'ACPI_NOTIFY_EVENT_ID', 'ACPI_TABLET_NOTIFY',
    'AcpiPhysicalSwitch', 'EtwBackend', 'HID_EVENT_FILTER_GUID',
    'WindowsHidEventFilterBackend', 'parse_acpi_notification',
]
