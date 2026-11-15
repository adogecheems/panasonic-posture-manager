"""Native service power callbacks are signal-only and safely rooted."""
from types import SimpleNamespace
import unittest

from panasonic_posture.windows.common.power import SuspendResumeSource


class PowerTests(unittest.TestCase):
    def api(self, *, registration_error=0):
        calls = []
        def register(flags, recipient, handle):
            self.assertEqual(flags, 2)
            if not registration_error:
                handle._obj.value = 101
            return registration_error
        def unregister(handle):
            calls.append(handle.value)
            return 0
        return SimpleNamespace(PowerRegisterSuspendResumeNotification=register,
                               PowerUnregisterSuspendResumeNotification=unregister), calls

    def test_callbacks_and_close(self):
        api, calls = self.api()
        events = []
        source = SuspendResumeSource(events.append, api=api)
        self.addCleanup(source.close)
        for kind in (4, 0x12, 7, 999):
            self.assertEqual(source.callback(None, kind, None), 0)
        self.assertEqual(events, [True, False, False])
        source.close()
        source.close()
        source.callback(None, 4, None)
        self.assertEqual(len(events), 3)
        self.assertEqual(calls, [101])
        self.assertNotIn(id(source), SuspendResumeSource._live)

    def test_registration_failure_does_not_leak_callback(self):
        api, calls = self.api(registration_error=5)
        before = dict(SuspendResumeSource._live)
        with self.assertRaises(OSError):
            SuspendResumeSource(lambda value: None, api=api)
        self.assertEqual(SuspendResumeSource._live, before)
        self.assertEqual(calls, [])

    def test_callback_failure_is_reported_and_cleanup_failure_retains_root(self):
        api, _ = self.api()
        def fail(value):
            raise OSError('fixture callback failure')
        source = SuspendResumeSource(fail, api=api)
        self.addCleanup(source.close)
        self.assertEqual(source.callback(None, 4, None), 13)
        self.assertIn('OSError', source.error)
        original = api.PowerUnregisterSuspendResumeNotification
        api.PowerUnregisterSuspendResumeNotification = lambda handle: 5
        with self.assertRaises(OSError):
            source.close()
        self.assertIs(SuspendResumeSource._live[id(source)], source)
        self.assertEqual(source.callback(None, 4, None), 0)
        api.PowerUnregisterSuspendResumeNotification = original
        source.close()
        self.assertNotIn(id(source), SuspendResumeSource._live)
