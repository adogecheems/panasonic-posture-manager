"""Kernel control receipts are bounded leases, never just a successful write."""
import ctypes as C
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from panasonic_posture.model import Target
from panasonic_posture.service.ports import ActuatorError
from panasonic_posture.windows.actuator import WindowsActuator, NativeControl, REQUEST, REPLY

TENT = Target(mode='tent', tablet=True, rotation=180, disable_internal_input=True)
HARDWARE = Target(mode='hardware', tablet=True, rotation=None, disable_internal_input=False)


class Driver:
    def __init__(self):
        self.closed = False
        self.calls = []
        self.status = 0
        self.corrupt = None
        self.fail_close = False

    def exchange(self, packet):
        version, size, operation, reserved, serial, low, high = REQUEST.unpack(packet)
        assert (version, size, reserved) == (1, 40, 0)
        self.calls.append(operation)
        blocked = operation in (2, 3) and self.status == 0
        fields = [1, 56, self.status, int(blocked), serial, low, high, 3,
                  3000 if blocked else 0, 1 if self.status == 3 else 0, 0]
        if self.corrupt:
            return self.corrupt(fields)
        return REPLY.pack(*fields)

    def close(self):
        if self.fail_close:
            raise OSError('injected close failure')
        self.closed = True


class ActuatorTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.driver = Driver()
        self.actuator = WindowsActuator(factory=lambda: self.driver, clock=lambda: self.now)
        self.addCleanup(self.actuator.release)

    def test_lazy_acquire_renew_receipt_and_handle_release(self):
        self.assertFalse(self.actuator.running)
        self.actuator.keep(self.now)
        self.assertEqual(self.driver.calls, [2])
        self.assertTrue(self.actuator.state.satisfies(TENT))
        self.actuator.keep(self.now + .1)
        self.assertEqual(self.driver.calls, [2])
        self.now += 1
        self.actuator.keep(self.now)
        self.assertEqual(self.driver.calls, [2, 3])
        self.actuator.release()
        self.assertTrue(self.driver.closed)
        self.assertFalse(self.actuator.running)
        self.assertTrue(self.actuator.state.satisfies(HARDWARE))
        self.assertFalse(self.actuator.state.satisfies(TENT))

    def test_busy_input_never_confirms_and_is_retried_without_reopening(self):
        self.driver.status = 3
        self.actuator.keep(self.now)
        self.assertFalse(self.actuator.state.satisfies(TENT))
        self.assertIsNone(self.actuator.error_code)
        self.driver.status = 0
        self.actuator.keep(self.now)
        self.assertTrue(self.actuator.state.satisfies(TENT))
        self.assertEqual(self.driver.calls, [2, 2])

    def test_expired_kernel_lease_cannot_be_silently_reacquired(self):
        self.actuator.keep(self.now)
        self.now += 4
        self.assertFalse(self.actuator.state.satisfies(TENT))
        self.driver.status = 4
        with self.assertRaises(ActuatorError):
            self.actuator.keep(self.now)
        self.assertTrue(self.driver.closed)
        self.assertEqual(self.driver.calls, [2, 3])
        self.assertIsNotNone(self.actuator.error_code)
        with self.assertRaises(ActuatorError):
            self.actuator.keep(self.now)

    def test_malformed_identity_lengths_status_and_flags_close_the_handle(self):
        def changed(index, value):
            def corrupt(fields):
                fields[index] = value
                return REPLY.pack(*fields)
            return corrupt
        cases = [lambda fields: b'', lambda fields: REPLY.pack(*fields) + b'x',
                 changed(0, 2), changed(1, 55), changed(2, 99), changed(3, 2),
                 changed(4, 0), changed(5, 0), changed(6, 0), changed(7, 1),
                 changed(8, 3001), changed(9, 8), changed(10, 1)]
        for corrupt in cases:
            driver = Driver()
            driver.corrupt = corrupt
            actuator = WindowsActuator(factory=lambda: driver, clock=lambda: self.now,
                                       nonce_factory=lambda: (100, 200))
            with self.subTest(corrupt=corrupt), self.assertRaises(ActuatorError):
                actuator.keep(self.now)
            self.assertTrue(driver.closed)
            self.assertFalse(actuator.running)
            self.assertFalse(actuator.state.satisfies(TENT))

    def test_delayed_reply_does_not_extend_the_kernel_lease(self):
        def corrupt(fields):
            self.now += 4
            return REPLY.pack(*fields)
        self.driver.corrupt = corrupt
        with self.assertRaises(ActuatorError):
            self.actuator.keep(self.now)
        self.assertTrue(self.driver.closed)

    def test_close_failure_is_not_reported_as_successful_release(self):
        self.actuator.keep(self.now)
        self.driver.fail_close = True
        with self.assertRaises(ActuatorError):
            self.actuator.release()
        self.assertEqual(self.actuator.error_code, 'native-state-recovery-failed')
        self.assertFalse(self.actuator.state.satisfies(HARDWARE))
        self.driver.fail_close = False
        self.actuator.retry()
        self.assertTrue(self.driver.closed)
        self.assertIsNone(self.actuator.error_code)

    def test_hardware_handoff_releases_software_without_faking_a_switch(self):
        self.actuator.keep(self.now)
        self.actuator.follow_hardware(self.now)
        self.assertTrue(self.driver.closed)
        self.assertTrue(self.actuator.state.satisfies(HARDWARE))
        self.assertIsNone(self.actuator.touch_activity)

    def test_clock_reversal_and_invalid_nonce_never_confirm_input_control(self):
        self.actuator.keep(self.now)
        self.now -= 1
        self.assertFalse(self.actuator.state.satisfies(TENT))
        for nonce in ((0, 0), (True, 1), (2**64, 1), (1,), None):
            driver = Driver()
            actuator = WindowsActuator(factory=lambda: driver, nonce_factory=lambda: nonce)
            with self.subTest(nonce=nonce), self.assertRaises(ActuatorError):
                actuator.keep(0)
            self.assertTrue(driver.closed)
            self.assertFalse(driver.calls)


class NativeControlTests(unittest.TestCase):
    def test_requires_system_before_attempting_to_open_the_driver(self):
        security = Mock()
        security.require_system.side_effect = PermissionError('not SYSTEM')
        with patch('panasonic_posture.windows.actuator.sys.platform', 'win32'), \
             patch('panasonic_posture.windows.security.WindowsSecurity', return_value=security):
            with self.assertRaises(PermissionError):
                NativeControl()
        security.kernel.CreateFileW.assert_not_called()

    def test_native_ioctl_uses_fixed_sizes_and_requires_a_complete_reply(self):
        calls = []
        def exchange(handle, ioctl, request, input_size, response, output_size, length, overlapped):
            calls.append((handle, ioctl, input_size, output_size, overlapped))
            C.memset(response, 0, output_size)
            length._obj.value = output_size
            return True
        native = object.__new__(NativeControl)
        native.handle = 12
        native.kernel = SimpleNamespace(DeviceIoControl=exchange, CloseHandle=Mock(return_value=True))
        self.assertEqual(len(native.exchange(b'x' * 40)), 56)
        self.assertEqual(calls, [(12, 0x22e000, 40, 56, None)])
        native.close()
        self.assertIsNone(native.handle)
        with self.assertRaises(OSError):
            native.exchange(b'x' * 40)
