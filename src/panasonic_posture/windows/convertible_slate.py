"""Narrow access to the verified laptop/slate GPIO indicator.

The Windows PDO consumes a one-byte write which toggles the indicator; it is
not an assignment API.  This module identifies the one supported device,
opens only its physical-device-object path, reads the current raw state before
every change, performs at most one toggle, and verifies the result by reading
it back.  It never writes the shell tablet mode and never patches the CSM
registry cache.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes
import sys
import time
from typing import Protocol

from .slate_journal import SlateJournal, SlateLease

SUPPORTED_INSTANCE_ID = r"ACPI\INT33D3\2&DABA3FF&0"
SUPPORTED_MANUFACTURER = "Panasonic Corporation"
SUPPORTED_PRODUCT = "CFQV8-1L"

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x00000080
_DEVPROP_TYPE_STRING = 0x00000012
_DIGCF_PRESENT = 0x00000002
_DIGCF_ALLCLASSES = 0x00000004
_INVALID_HANDLE_VALUE = C.c_void_p(-1).value
_ERROR_NO_MORE_ITEMS = 259
_SLATE_RAW = 0
_LAPTOP_RAW = 1
CSM_SETTLE_SECONDS = 2.0


class SlateError(OSError):
    """The supported convertible-slate indicator is unavailable or invalid."""


class GUID(C.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", C.c_ubyte * 8),
    ]


class DevInfoData(C.Structure):
    _fields_ = [
        ("size", wintypes.DWORD),
        ("class_guid", GUID),
        ("dev_inst", wintypes.DWORD),
        ("reserved", C.c_size_t),
    ]


class DevicePropertyKey(C.Structure):
    _fields_ = [("format_id", GUID), ("property_id", wintypes.DWORD)]


_DEVPKEY_DEVICE_PDO_NAME = DevicePropertyKey(
    GUID(
        0xA45C254E,
        0xDF1C,
        0x4EFD,
        (0x80, 0x20, 0x67, 0xD1, 0x46, 0xA8, 0x50, 0xE0),
    ),
    16,
)


class NativeSlateDevice:
    """Open and toggle the exact verified child device."""

    def __init__(
        self,
        *,
        expected_machine: tuple[str, str] = (SUPPORTED_MANUFACTURER, SUPPORTED_PRODUCT),
        expected_instance: str = SUPPORTED_INSTANCE_ID,
        sleep=time.sleep,
    ) -> None:
        if sys.platform != "win32":
            raise OSError("convertible-slate access requires Windows")
        self.expected_machine = expected_machine
        self.expected_instance = expected_instance
        self.sleep = sleep
        self.handle = None
        self._bind()
        try:
            self._verify_machine()
            pdo_name = self._physical_device_object_name(expected_instance)
            self.handle = self._open_pdo(pdo_name)
        except BaseException:
            self.close()
            raise

    def _bind(self) -> None:
        self.setup = C.WinDLL("setupapi", use_last_error=True)
        self.kernel = C.WinDLL("kernel32", use_last_error=True)

        def bind(dll, name, result, *args):
            function = getattr(dll, name)
            function.restype, function.argtypes = result, args
            return function

        self.SetupDiGetClassDevsW = bind(
            self.setup,
            "SetupDiGetClassDevsW",
            wintypes.HANDLE,
            C.c_void_p,
            wintypes.LPCWSTR,
            wintypes.HWND,
            wintypes.DWORD,
        )
        self.SetupDiEnumDeviceInfo = bind(
            self.setup,
            "SetupDiEnumDeviceInfo",
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.DWORD,
            C.POINTER(DevInfoData),
        )
        self.SetupDiGetDeviceInstanceIdW = bind(
            self.setup,
            "SetupDiGetDeviceInstanceIdW",
            wintypes.BOOL,
            wintypes.HANDLE,
            C.POINTER(DevInfoData),
            wintypes.LPWSTR,
            wintypes.DWORD,
            C.POINTER(wintypes.DWORD),
        )
        self.SetupDiGetDevicePropertyW = bind(
            self.setup,
            "SetupDiGetDevicePropertyW",
            wintypes.BOOL,
            wintypes.HANDLE,
            C.POINTER(DevInfoData),
            C.POINTER(DevicePropertyKey),
            C.POINTER(wintypes.DWORD),
            C.c_void_p,
            wintypes.DWORD,
            C.POINTER(wintypes.DWORD),
            wintypes.DWORD,
        )
        self.SetupDiDestroyDeviceInfoList = bind(
            self.setup, "SetupDiDestroyDeviceInfoList", wintypes.BOOL, wintypes.HANDLE
        )
        self.CreateFileW = bind(
            self.kernel,
            "CreateFileW",
            wintypes.HANDLE,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        self.ReadFile = bind(
            self.kernel,
            "ReadFile",
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            C.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        self.WriteFile = bind(
            self.kernel,
            "WriteFile",
            wintypes.BOOL,
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            C.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
        )
        self.CloseHandle = bind(
            self.kernel, "CloseHandle", wintypes.BOOL, wintypes.HANDLE
        )

    def _verify_machine(self) -> None:
        import winreg

        path = r"HARDWARE\DESCRIPTION\System\BIOS"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                                winreg.KEY_QUERY_VALUE) as key:
                values = {
                    name: winreg.QueryValueEx(key, name)[0]
                    for name in ("SystemManufacturer", "SystemProductName")
                }
        except OSError as error:
            raise SlateError("cannot identify the installed Windows machine") from error
        actual = (values["SystemManufacturer"], values["SystemProductName"])
        if actual != self.expected_machine:
            raise SlateError("convertible-slate control is not verified for this machine")
    def _physical_device_object_name(self, instance_id: str) -> str:
        handle = self.SetupDiGetClassDevsW(
            None, None, None, _DIGCF_PRESENT | _DIGCF_ALLCLASSES
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            raise SlateError(C.get_last_error(), "cannot enumerate Windows devices")
        try:
            found = []
            index = 0
            while True:
                info = DevInfoData()
                info.size = C.sizeof(info)
                if not self.SetupDiEnumDeviceInfo(handle, index, C.byref(info)):
                    error = C.get_last_error()
                    if error == _ERROR_NO_MORE_ITEMS:
                        break
                    raise SlateError(error, "cannot enumerate Windows devices")
                buffer = C.create_unicode_buffer(512)
                if not self.SetupDiGetDeviceInstanceIdW(
                    handle, C.byref(info), buffer, len(buffer), None
                ):
                    raise SlateError(C.get_last_error(), "cannot read device identity")
                if buffer.value.casefold() == instance_id.casefold():
                    found.append(info)
                index += 1
            if len(found) != 1:
                raise SlateError(
                    "verified convertible-slate device is absent or ambiguous"
                )
            size = wintypes.DWORD()
            buffer = C.create_unicode_buffer(512)
            kind = wintypes.DWORD()
            function = self.SetupDiGetDevicePropertyW
            if not function(
                handle,
                C.byref(found[0]),
                C.byref(_DEVPKEY_DEVICE_PDO_NAME),
                C.byref(kind),
                C.cast(buffer, C.c_void_p),
                C.sizeof(buffer),
                C.byref(size),
                0,
            ):
                raise SlateError(
                    C.get_last_error(), "cannot read the convertible-slate device path"
                )
            pdo = buffer.value
            if (
                kind.value != _DEVPROP_TYPE_STRING
                or not 2 <= size.value <= C.sizeof(buffer)
                or not pdo.startswith("\\Device\\")
                or len(pdo) > 256
                or any(char in pdo for char in "\0\r\n")
            ):
                raise SlateError("the convertible-slate device path is invalid")
            return pdo
        finally:
            self.SetupDiDestroyDeviceInfoList(handle)

    def _open_pdo(self, pdo_name: str):
        path = r"\\.\GLOBALROOT" + pdo_name
        handle = self.CreateFileW(
            path,
            _GENERIC_READ | _GENERIC_WRITE,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            error = C.get_last_error()
            raise SlateError(
                error,
                "cannot open the convertible-slate indicator; configure "
                "LaptopSlateExclusiveAccess=0 and restart Windows",
            )
        return handle

    def close(self) -> None:
        if getattr(self, "handle", None) not in (None, _INVALID_HANDLE_VALUE):
            handle = self.handle
            self.handle = None
            if not self.CloseHandle(handle):
                raise OSError(C.get_last_error(), "cannot close convertible-slate device")

    def _raw_read(self) -> int:
        if self.handle is None:
            raise SlateError("convertible-slate device is closed")
        data, count = C.create_string_buffer(1), wintypes.DWORD()
        if not self.ReadFile(self.handle, data, 1, C.byref(count), None):
            raise SlateError(C.get_last_error(), "convertible-slate read failed")
        if count.value != 1 or data.raw[0] not in (_LAPTOP_RAW, _SLATE_RAW):
            raise SlateError("convertible-slate returned an invalid state")
        return data.raw[0]

    def read(self) -> bool:
        """Return True for slate/tablet use and False for laptop use."""

        return self._raw_read() == _SLATE_RAW

    def set(self, slate: bool) -> bool:
        """Idempotently set slate state and verify at most one toggle."""

        if type(slate) is not bool:
            raise ValueError("slate state must be boolean")
        expected = _SLATE_RAW if slate else _LAPTOP_RAW
        before = self._raw_read()
        if before == expected:
            return False
        data, count = C.create_string_buffer(b"\x01", 1), wintypes.DWORD()
        if not self.WriteFile(self.handle, data, 1, C.byref(count), None):
            raise SlateError(C.get_last_error(), "convertible-slate write failed")
        if count.value != 1:
            raise SlateError("convertible-slate write was not accepted")
        for _ in range(10):
            if self._raw_read() == expected:
                return True
            self.sleep(0.05)
        raise SlateError("convertible-slate read-back did not match the request")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()


class SlateDevice(Protocol):
    def read(self) -> bool: ...
    def set(self, slate: bool) -> bool: ...
    def close(self) -> None: ...


class SlateNotifier:
    """Apply app posture independently from the display transaction."""

    def __init__(
        self,
        device: SlateDevice,
        journal: SlateJournal | None = None,
        *,
        clock=time.monotonic,
    ) -> None:
        self.device = device
        self.journal = journal
        self.clock = clock
        self.original: bool | None = None
        self.last: bool | None = None
        self.pending: SlateLease | None = None
        self._expected: bool | None = None
        self._settle_until = float('-inf')
        self.closed = False
        try:
            self.pending = journal.load() if journal is not None else None
        except BaseException:
            device.close()
            raise
        try:
            current = bool(device.read())
            self.original = self.pending.original if self.pending is not None else current
            self.last = current
        except (OSError, ValueError):
            # Keep the backend's primary failure at the first posture request.
            if self.pending is not None:
                self.original = self.pending.original

    @staticmethod
    def desired(mode: str) -> bool | None:
        if mode in ("tent", "stand", "tablet", "hardware"):
            return True
        if mode == "laptop":
            return False
        return None

    @property
    def software_owned(self) -> bool | None:
        """Return the slate value changed by this notifier, if any."""
        if self.pending is not None:
            # Recovery has not reconciled the interrupted write yet, so its
            # current CSM cannot establish software ownership.
            return None
        if self._expected is not None and self.clock() < self._settle_until:
            # Device readback is synchronous, but Windows' CSM notification is
            # asynchronous. Keep the just-written value authoritative for a
            # bounded handoff window.
            return self._expected
        self._expected = None
        if self.original is None or self.last is None or self.last == self.original:
            return None
        return self.last

    @property
    def software_settling(self) -> bool:
        return self._expected is not None and self.clock() < self._settle_until

    def _mark_settle(self, desired: bool) -> None:
        self._expected = desired
        self._settle_until = self.clock() + CSM_SETTLE_SECONDS

    def sync_mode(self, mode: str) -> bool:
        if self.closed:
            raise SlateError("convertible-slate notifier is closed")
        desired = self.desired(mode)
        if desired is None:
            return False
        if self.pending is not None:
            return self._coordinate_recovery(desired)
        if desired == self.last:
            return False
        if self.original is None:
            self.original = bool(self.device.read())
        if desired == self.original:
            changed = bool(self.device.set(desired))
            self._mark_settle(desired)
            if self.journal is not None:
                self.journal.clear()
        else:
            before = bool(self.device.read())
            if self.journal is not None:
                self.journal.save(self.original, desired)
            changed = bool(self.device.set(desired))
            if changed:
                self._mark_settle(desired)
            elif before == desired:
                # The user already changed the physical indicator to this
                # target. Adopt it as a new external baseline instead of
                # claiming software ownership of a value we did not write.
                self.original = desired
                if self.journal is not None:
                    self.journal.clear()
        self.last = desired
        return changed

    def _coordinate_recovery(self, desired: bool) -> bool:
        """Reconcile a prior lease only once the current posture is known."""

        pending = self.pending
        if pending is None:
            return False
        current = bool(self.device.read())
        retain_lease = False
        if current == desired:
            # A restart in the posture whose notification was interrupted
            # adopts the observable state. Replaying the old startup value
            # here would produce a false laptop-to-slate flash.
            self.original = current
            changed = False
        else:
            self.original = pending.original
            retain_lease = desired != self.original
            if retain_lease and self.journal is not None:
                # The user may have changed posture before the first request
                # after a crash. Persist the new destination before one toggle.
                self.journal.save(self.original, desired)
            changed = bool(self.device.set(desired))
            self._mark_settle(desired)
        self.last = desired
        self.pending = None
        if self.journal is not None and not retain_lease:
            self.journal.clear()
        return changed

    def release(self) -> None:
        """Return the indicator to its startup value, keeping the lease reusable."""

        if self.closed:
            return
        if self.pending is not None:
            original = self.pending.original
            self.device.set(original)
            self._mark_settle(original)
            self.pending = None
            self.original = original
            self.last = original
            if self.journal is not None:
                self.journal.clear()
            return
        if self.original is None:
            return
        self.device.set(self.original)
        self._mark_settle(self.original)
        if self.journal is not None:
            self.journal.clear()
        self.last = self.original

    def close(self) -> None:
        if self.closed:
            return
        try:
            self.release()
        finally:
            self.closed = True
            self.device.close()


__all__ = [
    "NativeSlateDevice",
    "SUPPORTED_INSTANCE_ID",
    "SUPPORTED_MANUFACTURER",
    "SUPPORTED_PRODUCT",
    "SlateError",
    "SlateNotifier",
]
