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
    def test_read_only_startup_baseline_seeds_an_unobserved_switch(self):
        backend = FakeBackend()
        switch = AcpiPhysicalSwitch(backend, initial_reader=lambda: False)
        switch.start()
        switch.bootstrap()
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        switch.close()

    def test_etw_event_during_bootstrap_wins_over_the_baseline(self):
        backend = FakeBackend()

        def bootstrap():
            backend.emit(0xC0, ACPI_TABLET_NOTIFY)
            return False

        switch = AcpiPhysicalSwitch(backend, initial_reader=bootstrap)
        switch.start()
        switch.bootstrap()
        self.assertEqual(switch.query(), (True, HardwareStatus.OK))
        switch.close()

    def test_failed_bootstrap_does_not_invent_physical_state(self):
        backend = FakeBackend()

        def bootstrap():
            raise OSError('indicator unavailable')

        with self.assertLogs('panasonic_posture.windows.etw_switch', level='WARNING'):
            switch = AcpiPhysicalSwitch(backend, initial_reader=bootstrap)
            switch.start()
            switch.bootstrap()
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        switch.close()

    def test_startup_finalization_never_leaves_an_unknown_baseline(self):
        backend = FakeBackend()
        switch = AcpiPhysicalSwitch(backend, initial_reader=lambda: None)
        switch.start()
        switch.bootstrap()
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        switch.finalize_bootstrap(False)
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        switch.close()

    def test_state_is_unknown_until_an_independent_etw_event_arrives(self):
        backend = FakeBackend()
        switch = AcpiPhysicalSwitch(backend)
        switch.start()
        self.assertTrue(backend.started)
        self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        backend.emit(0xC0, ACPI_TABLET_NOTIFY)
        self.assertEqual(switch.query(), (True, HardwareStatus.OK))
        backend.emit(0xC0, ACPI_LAPTOP_NOTIFY)
        self.assertEqual(switch.query(), (False, HardwareStatus.OK))
        switch.close()
        self.assertTrue(backend.closed)

    def test_unknown_or_duplicate_payloads_do_not_publish_a_state(self):
        backend = FakeBackend()
        switch = AcpiPhysicalSwitch(backend)
        switch.start()
        for words in ((0xC0, 0xCF), (0xC0, ACPI_TABLET_NOTIFY, ACPI_LAPTOP_NOTIFY)):
            backend.emit(*words)
            self.assertEqual(switch.query(), (None, HardwareStatus.ABSENT))
        switch.close()

    def test_backend_failure_degrades_to_unknown(self):
        backend = FakeBackend()
        switch = AcpiPhysicalSwitch(backend)
        switch.start()
        backend.emit(0xC0, ACPI_LAPTOP_NOTIFY)
        backend.failure = OSError('lost')
        self.assertEqual(switch.query(), (None, HardwareStatus.READ_ERROR))
        switch.close()

    def test_permission_failure_does_not_publish_a_physical_state(self):
        class Denied(FakeBackend):
            def start(self, callback):
                raise PermissionError('not LocalSystem')

        backend = Denied()
        switch = AcpiPhysicalSwitch(backend)
        switch.start()
        self.assertEqual(switch.query(), (None, HardwareStatus.PERMISSION))


if __name__ == '__main__':
    unittest.main()
