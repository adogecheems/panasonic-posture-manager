"""Session/power observation must not turn unknown or stale state into ready."""
from concurrent.futures import Future
import ctypes as C
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.session.display import transient_context_blip
from panasonic_posture.windows.common.security import ExtendedInfo, HANDLE
from panasonic_posture.windows.session.context import (
    SessionPresence, WindowsSessionContext, NativePowerSource, NativeSessionPresence,
    SubscribeParameters, PowerHeader, DisplayPowerSetting, SESSION_DISPLAY_GUID,
)


class Power:
    def __init__(self, callback):
        self.callback = callback
        self.closed = False
        callback('display', 1)

    def close(self):
        self.closed = True


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.
        self.presence = SessionPresence(1, 1, True, False)
        self.sources = []

    def context(self, **kwargs):
        def factory(callback):
            source = Power(callback)
            self.sources.append(source)
            return source
        context = WindowsSessionContext(query=lambda: self.presence, power_factory=factory,
            clock=lambda: self.now, executor=kwargs.pop('executor', InlineExecutor()), **kwargs)
        self.addCleanup(context.close)
        return context

    def test_only_stale_presence_is_a_transient_control_loss(self):
        context = self.context()
        context.poll()
        self.assertFalse(transient_context_blip(context))
        self.now += 1.1
        self.assertFalse(context.control_available())
        self.assertTrue(transient_context_blip(context))
        context.poll()
        self.sources[-1].error = 'fixture observer failure'
        self.assertFalse(context.control_available())
        self.assertFalse(transient_context_blip(context))
        self.sources[-1].error = None
        self.presence = SessionPresence(1, 2, True, False)
        self.now += .6
        context.poll()
        self.assertFalse(context.control_available())
        self.assertFalse(transient_context_blip(context))
        self.presence = SessionPresence(1, 1, True, True)
        self.now += .6
        context.poll()
        self.assertFalse(transient_context_blip(context))
        self.presence = SessionPresence(1, 1, True, False)
        self.now += .6
        context.poll()
        self.sources[-1].callback('suspend', None)
        self.assertFalse(transient_context_blip(context))

    def test_only_fresh_unlocked_local_console_and_known_on_display_are_attended(self):
        context = self.context()
        self.assertTrue(context.unattended())
        context.poll()
        self.assertFalse(context.unattended())
        for value in (SessionPresence(1, 2, True, False), SessionPresence(1, 1, False, False),
                      SessionPresence(1, 1, True, True)):
            self.presence = value
            self.now += .6
            context.poll()
            self.assertTrue(context.unattended())
        self.presence = SessionPresence(1, 1, True, False)
        self.now += .6
        context.poll()
        self.assertFalse(context.unattended())
        self.sources[-1].callback('display', 0)
        self.assertTrue(context.unattended())
        self.assertTrue(context.control_available())
        self.sources[-1].callback('display', 2)
        self.assertFalse(context.unattended())

    def test_missing_initial_display_callback_is_recovered_by_resubscription(self):
        sources = []

        class NoInitialPower:
            def __init__(self, callback):
                self.callback, self.closed = callback, False

            def close(self):
                self.closed = True

        def factory(callback):
            source = NoInitialPower(callback)
            sources.append(source)
            if len(sources) == 2:
                callback('display', 1)
            return source

        context = WindowsSessionContext(
            query=lambda: self.presence,
            power_factory=factory,
            clock=lambda: self.now,
            executor=InlineExecutor(),
        )
        self.addCleanup(context.close)
        context.poll()
        self.assertFalse(context.control_available())
        self.now += 1.1
        context.poll()
        self.assertEqual(len(sources), 2)
        self.assertTrue(sources[0].closed)
        self.assertTrue(context.control_available())

    def test_query_failure_and_unknown_power_do_not_inherit_previous_readiness(self):
        context = self.context()
        context.poll()
        context.query = lambda: (_ for _ in ()).throw(OSError('fixture WTS unavailable'))
        self.now += .6
        context.poll()
        self.assertTrue(context.unattended())
        context.query = lambda: self.presence
        self.now += .6
        context.poll()
        self.assertFalse(context.unattended())
        self.sources[-1].callback('display', 7)
        self.assertTrue(context.unattended())

    def test_failed_power_observer_cannot_retain_an_old_on_value(self):
        context = self.context()
        context.poll()
        self.assertFalse(context.unattended())
        self.sources[-1].error = 'fixture callback failed before notifying context'
        self.assertTrue(context.unattended())
        self.assertIn('callback failed', context.snapshot()['error'])

    def test_stalled_queries_expire_without_spawning_more_work(self):
        jobs = []
        def submit(query):
            future = Future()
            jobs.append(future)
            return future
        context = self.context(executor=SimpleNamespace(submit=submit))
        context.poll()
        self.assertEqual(len(jobs), 1)
        self.now += 2
        for _ in range(20):
            context.poll()
        self.assertTrue(context.unattended())
        self.assertEqual(len(jobs), 1)
        jobs[0].set_result(self.presence)
        context.poll()
        self.assertTrue(context.unattended())
        self.assertEqual(len(jobs), 2)
        jobs[1].set_result(self.presence)
        context.poll()
        self.assertFalse(context.unattended())

    def test_suspend_invalidates_inflight_query_and_resume_requeries_power(self):
        context = self.context()
        context.poll()
        old = self.sources[-1]
        old.callback('suspend', None)
        self.assertTrue(context.unattended())
        old.callback('display', 1)
        self.assertTrue(context.unattended())
        old.callback('resume', None)
        self.assertTrue(context.unattended())
        context.poll()
        self.assertTrue(old.closed)
        self.assertEqual(len(self.sources), 2)
        self.assertFalse(context.unattended())
        old.callback('display', 0)
        old.callback('suspend', None)
        self.assertFalse(context.unattended())

    def test_close_and_clock_reversal_fail_closed(self):
        context = self.context()
        context.poll()
        self.now -= 1
        self.assertTrue(context.unattended())
        context.close()
        self.sources[-1].callback('display', 1)
        self.assertTrue(context.unattended())
        context.poll()
        self.assertTrue(self.sources[-1].closed)

    def test_a_slow_inline_query_does_not_recursively_start_more_queries(self):
        calls = []
        context = self.context()
        def slow():
            calls.append(True)
            self.now += 2
            return self.presence
        context.query = slow
        context.poll()
        self.assertTrue(context.unattended())
        self.assertEqual(calls, [True])

    def test_pre_suspend_inflight_query_cannot_requalify_after_resume(self):
        jobs = []
        def submit(query):
            future = Future()
            jobs.append(future)
            return future
        context = self.context(executor=SimpleNamespace(submit=submit))
        context.poll()
        self.sources[-1].callback('suspend', None)
        self.sources[-1].callback('resume', None)
        jobs[0].set_result(self.presence)
        context.poll()
        self.assertTrue(context.unattended())
        self.assertEqual(len(jobs), 2)
        jobs[1].set_result(self.presence)
        context.poll()
        self.assertFalse(context.unattended())


class NativePowerTests(unittest.TestCase):
    def api(self, *, fail_second=False):
        calls = []
        def setting(guid, flags, recipient, handle):
            self.assertEqual(C.string_at(guid, 16), SESSION_DISPLAY_GUID)
            self.assertEqual(flags, 2)
            handle._obj.value = 101
            return 0
        def suspend(flags, recipient, handle):
            if fail_second:
                return 5
            handle._obj.value = 102
            return 0
        def remove(handle):
            calls.append(handle)
            return 0
        return SimpleNamespace(PowerSettingRegisterNotification=setting,
            PowerRegisterSuspendResumeNotification=suspend,
            PowerSettingUnregisterNotification=remove,
            PowerUnregisterSuspendResumeNotification=remove), calls

    def packet(self, *, length=4, value=1):
        packet = DisplayPowerSetting()
        packet.header.guid[:] = SESSION_DISPLAY_GUID
        packet.header.length, packet.value = length, value
        return packet

    def test_native_layout_notifications_and_cleanup(self):
        self.assertEqual(C.sizeof(PowerHeader), 20)
        self.assertEqual(C.sizeof(DisplayPowerSetting), 24)
        self.assertEqual(C.sizeof(SubscribeParameters), 2 * C.sizeof(HANDLE))
        api, calls = self.api()
        events = []
        source = NativePowerSource(lambda *event: events.append(event), api=api)
        self.addCleanup(source.close)
        for value in (0, 1, 2):
            packet = self.packet(value=value)
            self.assertEqual(source.callback(None, 0x8013, C.addressof(packet)), 0)
        for kind in (4, 7, 0x12):
            self.assertEqual(source.callback(None, kind, None), 0)
        self.assertEqual(events, [('display', 0), ('display', 1), ('display', 2),
                                  ('suspend', None), ('resume', None), ('resume', None)])
        source.close()
        self.assertEqual(calls, [102, 101])
        self.assertNotIn(id(source), NativePowerSource._live)
        source.callback(None, 4, None)
        self.assertEqual(len(events), 6)

    def test_malformed_power_callbacks_report_unknown_without_escaping_ctypes(self):
        api, _ = self.api()
        events = []
        source = NativePowerSource(lambda *event: events.append(event), api=api)
        self.addCleanup(source.close)
        for packet in (self.packet(length=0), self.packet(value=7), None):
            self.assertEqual(source.callback(None, 0x8013, C.addressof(packet) if packet else None), 13)
        self.assertEqual(events, [('error', None)] * 3)

    def test_partial_registration_failure_releases_the_first_registration(self):
        api, calls = self.api(fail_second=True)
        before = dict(NativePowerSource._live)
        with self.assertRaises(OSError):
            NativePowerSource(lambda *event: None, api=api)
        self.assertEqual(calls, [101])
        self.assertEqual(NativePowerSource._live, before)

    def test_failed_unregister_keeps_the_ctypes_trampoline_alive_until_retry(self):
        api, _ = self.api()
        source = NativePowerSource(lambda *event: None, api=api)
        self.addCleanup(source.close)
        api.PowerSettingUnregisterNotification = lambda handle: 5
        with self.assertRaises(OSError):
            source.close()
        self.assertIs(NativePowerSource._live[id(source)], source)
        api.PowerSettingUnregisterNotification = lambda handle: 0
        source.close()
        self.assertNotIn(id(source), NativePowerSource._live)


class NativePresenceTests(unittest.TestCase):
    def query(self, *, fail=False, short=False, flags=1, level=1, console=None):
        query = object.__new__(NativeSessionPresence)
        query.session_id = 1
        info = ExtendedInfo()
        info.level = level
        info.data.session, info.data.state, info.data.flags = 1, 0, flags
        freed = []
        ids = iter(console or [1, 1])
        def read(server, session, kind, buffer, size):
            buffer._obj.value = C.addressof(info)
            size._obj.value = 1 if short else C.sizeof(info)
            return not fail
        query.kernel = SimpleNamespace(WTSGetActiveConsoleSessionId=lambda: next(ids))
        query.wts = SimpleNamespace(WTSQuerySessionInformationW=read,
                                   WTSFreeMemory=lambda handle: freed.append(handle.value))
        return query, freed

    def test_console_switch_during_query_and_locked_session_are_unattended(self):
        for changes, attended in (({}, True), ({'flags': 0}, False), ({'console': [1, 2]}, False)):
            query, freed = self.query(**changes)
            self.assertEqual(query().attended, attended)
            self.assertEqual(len(freed), 1)

    def test_invalid_or_failed_wts_result_is_freed_and_cannot_qualify(self):
        for changes in ({'fail': True}, {'short': True}, {'flags': -1}, {'level': 2}):
            query, freed = self.query(**changes)
            with patch.object(C, 'get_last_error', return_value=5, create=True):
                with self.assertRaises(OSError):
                    query()
            self.assertEqual(len(freed), 1)
