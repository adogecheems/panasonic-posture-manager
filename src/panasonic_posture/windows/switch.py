"""Read physical slate state only after authenticating a present GPIO device.

Never write ConvertibleSlateMode: it is physical observation, not software
tablet intent. Default GetSystemMetrics values on non-convertibles are not
evidence of a switch. The ordinary software preview does not load this port.
"""
import ctypes as C
import sys
import uuid

from ..model import HardwareStatus

ACPI_BUS = uuid.UUID('d7b46895-001a-4942-891f-a7d46610a843').bytes_le
HID_CLASS = '{745a17a0-74d3-11d0-b6fe-00a0c90f57da}'


class NativeGPIO:
    def __init__(self):
        if sys.platform != 'win32':
            raise OSError('GPIO slate state requires Windows')
        self.cm = C.WinDLL('cfgmgr32', use_last_error=True)
        self.user = C.WinDLL('user32', use_last_error=True)
        p = C.POINTER
        for function, result, args in (
            (self.cm.CM_Locate_DevNodeW, C.c_uint32, [p(C.c_uint32), C.c_wchar_p, C.c_uint32]),
            (self.cm.CM_Get_DevNode_Status, C.c_uint32, [p(C.c_uint32), p(C.c_uint32), C.c_uint32, C.c_uint32]),
            (self.cm.CM_Get_DevNode_Registry_PropertyW, C.c_uint32,
             [C.c_uint32, C.c_uint32, p(C.c_uint32), C.c_void_p, p(C.c_uint32), C.c_uint32]),
            (self.user.GetSystemMetrics, C.c_int32, [C.c_int32]),
        ):
            function.restype, function.argtypes = result, args

    def candidates(self):
        import winreg
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                r'SYSTEM\CurrentControlSet\Enum\ACPI\INT33D3')
        except FileNotFoundError:
            return []
        found = []
        with key:
            count = winreg.QueryInfoKey(key)[0]
            if count > 32:
                raise OSError('too many GPIO switch instances')
            for index in range(count):
                instance = winreg.EnumKey(key, index)
                identity = 'ACPI\\INT33D3\\' + instance
                node = C.c_uint32()
                if self.cm.CM_Locate_DevNodeW(C.byref(node), identity, 0):
                    continue  # An old registry entry is not a present device.
                status, problem = C.c_uint32(), C.c_uint32()
                if self.cm.CM_Get_DevNode_Status(C.byref(status), C.byref(problem), node, 0):
                    raise OSError('cannot read GPIO device status')
                if not status.value & 8 or problem.value:
                    raise OSError('GPIO switch driver is not ready')
                with winreg.OpenKey(key, instance) as device:
                    service = winreg.QueryValueEx(device, 'Service')[0]
                    device_class = winreg.QueryValueEx(device, 'ClassGUID')[0]
                    ids = winreg.QueryValueEx(device, 'HardwareID')[0]
                if (not isinstance(service, str) or not isinstance(device_class, str) or
                        service.casefold() != 'msgpiowin32' or device_class.casefold() != HID_CLASS or
                        not isinstance(ids, list) or not all(isinstance(value, str) for value in ids) or
                        'ACPI\\INT33D3' not in [value.upper() for value in ids]):
                    raise OSError('unexpected GPIO device binding')
                bus, size, kind = (C.c_ubyte * 16)(), C.c_uint32(16), C.c_uint32()
                if (self.cm.CM_Get_DevNode_Registry_PropertyW(node, 0x14, C.byref(kind), bus, C.byref(size), 0)
                        or kind.value != 3 or size.value != 16 or bytes(bus) != ACPI_BUS):
                    raise OSError('GPIO switch is not on the internal ACPI bus')
                found.append(identity)
        return found

    def metric(self):
        return self.user.GetSystemMetrics(0x2003)


class WindowsSwitch:
    def __init__(self, native=None):
        self.native = native if native is not None else NativeGPIO()
        self.identity = None

    def query(self):
        try:
            candidates = self.native.candidates()
            if not candidates:
                return None, HardwareStatus.ABSENT
            if len(candidates) != 1:
                return None, HardwareStatus.AMBIGUOUS
            if self.identity is not None and self.identity != candidates[0]:
                return None, HardwareStatus.READ_ERROR
            self.identity = candidates[0]
            metric = self.native.metric()
            if type(metric) is not int or metric not in (0, 1):
                return None, HardwareStatus.READ_ERROR
            # Verified CFQV8 firmware reports 0 for laptop and 1 for slate.
            return metric == 1, HardwareStatus.OK
        except PermissionError:
            return None, HardwareStatus.PERMISSION
        except (OSError, ValueError, TypeError):
            return None, HardwareStatus.READ_ERROR
