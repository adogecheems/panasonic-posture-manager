"""The launcher alone can set the stop event; children only wait on it."""
import ctypes as C
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from panasonic_posture.windows.common.stop_event import NativeStopSignal, PREFIX


class Security:
    def __init__(self):
        self.system = True
        self.owner_system = True
        self.exists = False
        self.error = 0
        self.signaled = False
        self.closed = []
        self.calls = []
        def create(attributes, manual, initial, name):
            self.calls.append(('create', manual, initial, attributes._obj.inherit))
            self.error = 183 if self.exists else 0
            return 21
        def open_event(rights, inherit, name):
            self.calls.append(('open', rights, inherit))
            return 22
        def descriptor(text, version, target, length):
            self.calls.append(('sddl', text))
            target._obj.value = 12
            return True
        self.kernel = SimpleNamespace(CreateEventW=create, OpenEventW=open_event,
            SetEvent=lambda handle: setattr(self, 'signaled', True) or True,
            WaitForSingleObject=lambda handle, timeout: 0 if self.signaled else 258,
            CloseHandle=lambda handle: self.closed.append(handle) or True, LocalFree=lambda handle: None)
        self.advapi = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=descriptor)
    def _fn(self, dll, name, *signature):
        return getattr(dll, name)
    def _check(self, value):
        if not value:
            raise OSError('fixture API failure')
        return value
    def require_system(self):
        if not self.system:
            raise PermissionError('fixture requires SYSTEM')
    def require_system_object(self, handle, description):
        if not self.owner_system:
            raise PermissionError('fixture untrusted owner')


class StopSignalTests(unittest.TestCase):
    def setUp(self):
        self.security = Security()
        self.enterContext(patch.object(C, 'get_last_error', side_effect=lambda: self.security.error, create=True))
        self.enterContext(patch.object(C, 'set_last_error', side_effect=lambda value: setattr(self.security, 'error', value), create=True))

    def test_system_creates_an_exclusive_noninherited_event_with_user_wait_rights(self):
        signal = NativeStopSignal.create('S-1-5-21-1000', security=self.security)
        self.addCleanup(signal.close)
        self.assertTrue(signal.name.startswith(PREFIX))
        self.assertIn(('create', True, False, 0), self.security.calls)
        text = self.security.calls[0][1]
        self.assertIn('(A;;0x120000;;;S-1-5-21-1000)', text)
        self.assertFalse(signal.is_set())
        signal.set()
        self.assertTrue(signal.is_set())
        signal.close()
        self.assertEqual(self.security.closed, [21])

    def test_user_handle_can_wait_but_cannot_set_or_create(self):
        self.security.system = False
        with self.assertRaises(PermissionError):
            NativeStopSignal.create('S-1-5-21-1000', security=self.security)
        self.assertFalse(self.security.calls)
        signal = NativeStopSignal.open(PREFIX + 'a' * 32, security=self.security)
        self.addCleanup(signal.close)
        self.assertIn(('open', 0x120000, False), self.security.calls)
        with self.assertRaises(PermissionError):
            signal.set()
        self.security.signaled = True
        self.assertTrue(signal.is_set())

    def test_name_collision_and_untrusted_owner_are_closed_without_adoption(self):
        self.security.exists = True
        with self.assertRaises(PermissionError):
            NativeStopSignal.create('S-1-5-21-1000', security=self.security)
        self.assertEqual(self.security.closed, [21])
        self.security.owner_system = False
        with self.assertRaises(PermissionError):
            NativeStopSignal.open(PREFIX + 'b' * 32, security=self.security)
        self.assertEqual(self.security.closed, [21, 22])

    def test_invalid_names_and_sid_injection_fail_before_any_native_call(self):
        for name in ('arbitrary', PREFIX + '../user-event', PREFIX + 'A' * 32, PREFIX + 'a' * 31):
            with self.assertRaises(ValueError):
                NativeStopSignal.open(name, security=self.security)
        with self.assertRaises(ValueError):
            NativeStopSignal.create('S-1-5-21-1)(A;;GA;;;WD)', security=self.security)
        self.assertFalse(self.security.calls)
