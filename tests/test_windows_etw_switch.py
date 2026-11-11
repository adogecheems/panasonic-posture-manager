"""ETW physical-switch parsing and fail-closed behavior."""
import struct
import unittest

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.etw_switch import (
    ACPI_LAPTOP_NOTIFY, ACPI_TABLET_NOTIFY, AcpiPhysicalSwitch,
    _EventHeader, _EventRecord, parse_acpi_notification,
)


class FakeBackend:
    def __init__(self):
        self.callback = None
        self.started = False
        self.closed = False
        self.failure = None

    def start(self, callback):
        self.callback = callback
        self.started = True

    def close(self):
        self.closed = True

    def emit(self, *words):
        self.callback(struct.pack('<' + 'I' * len(words), *words))


class PayloadTests(unittest.TestCase):
    def test_windows_x64_etw_layout_assumptions(self):
        self.assertEqual(__import__('ctypes').sizeof(_EventHeader), 80)
        self.assertEqual(__import__('ctypes').sizeof(_EventRecord), 112)

    def test_only_unambiguous_acpi_transition_is_accepted(self):
        self.assertTrue(parse_acpi_notification(
            struct.pack('<II', 0xC0, ACPI_TABLET_NOTIFY)))
        self.assertFalse(parse_acpi_notification(
            struct.pack('<II', 0xC0, ACPI_LAPTOP_NOTIFY)))
        for payload in (b'', b'\xcc', struct.pack('<II', 0xC0, 0xCF),
                        struct.pack('<III', 0xC0, ACPI_TABLET_NOTIFY, ACPI_LAPTOP_NOTIFY)):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_acpi_notification(payload))

    def test_real_provider_message_prefix_is_not_mistaken_for_state(self):
        tablet = (
            'Received ACPI Notification\0\0'.encode('utf-16-le')
            + struct.pack('<I', ACPI_TABLET_NOTIFY)
        )
        laptop = (
            'Received ACPI Notification\0\0'.encode('utf-16-le')
            + struct.pack('<I', ACPI_LAPTOP_NOTIFY)
        )
        self.assertTrue(parse_acpi_notification(tablet))
        self.assertFalse(parse_acpi_notification(laptop))
        for value in (0xCA, 0xCB):
            self.assertIsNone(parse_acpi_notification(
                'Received ACPI Notification\0\0'.encode('utf-16-le')
                + struct.pack('<I', value)))


class PhysicalSwitchTests(unittest.TestCase):
    def test_baseline_is_used_until_an_independent_etw_event_arrives(self):
        backend = FakeBackend()
        switch = AcpiPhysicalSwitch(backend, baseline=lambda: (False, HardwareStatus.OK))
        switch.start()
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        backend.emit(0xC0, ACPI_TABLET_NOTIFY)
        self.assertEqual(switch.query(), (True, HardwareStatus.OK))
        backend.emit(0xC0, ACPI_LAPTOP_NOTIFY)
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        switch.close()
        self.assertTrue(backend.closed)

    def test_missing_or_failed_baseline_never_starts_the_consumer(self):
        for value, status in ((None, HardwareStatus.ABSENT),
                              (None, HardwareStatus.READ_ERROR)):
            backend = FakeBackend()
            switch = AcpiPhysicalSwitch(backend, baseline=lambda value=value, status=status: (value, status))
            switch.start()
            self.assertFalse(backend.started)
            self.assertEqual(switch.query(), (None, status))
            switch.close()

    def test_backend_failure_degrades_to_unknown(self):
        backend = FakeBackend()
        switch = AcpiPhysicalSwitch(backend, baseline=lambda: (False, HardwareStatus.OK))
        switch.start()
        backend.failure = OSError('lost')
        self.assertEqual(switch.query(), (None, HardwareStatus.READ_ERROR))
        switch.close()

    def test_permission_failure_does_not_publish_a_physical_state(self):
        class Denied(FakeBackend):
            def start(self, callback):
                raise PermissionError('not LocalSystem')

        backend = Denied()
        switch = AcpiPhysicalSwitch(backend, baseline=lambda: (False, HardwareStatus.OK))
        switch.start()
        self.assertEqual(switch.query(), (None, HardwareStatus.PERMISSION))


if __name__ == '__main__':
    unittest.main()
