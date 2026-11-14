"""Suspended native launch: ordinary token, verified identity, explicit resume."""
from pathlib import Path
import ctypes as C
from types import SimpleNamespace
import unittest

from panasonic_posture.windows.service.host import ConsoleIdentity
from panasonic_posture.windows.common.security import ExtendedInfo, HANDLE
from panasonic_posture.windows.service.launch import (
    ConsoleUser, NativeSessionLauncher, NativeLaunchAPI, StartupInfo, ProcessInfo,
    ExtendedLimits, CREATE_FLAGS,
)


USER = ConsoleUser('S-1-5-21-1000', 100, 1, True)
PEER = ConsoleIdentity(USER.sid, USER.logon_id, USER.session_id, 42, 12345)


class Signal:
    name = 'Global\\PanasonicPostureManager.Stop.' + 'a' * 32
    def __init__(self, events): self.events, self.signaled = events, False
    def is_set(self): return self.signaled
    def set(self): self.events.append('stop'); self.signaled = True
    def close(self): self.events.append('signal-close')


class Runtime:
    root = Path('C:/Program Files/PanasonicPostureManager')
    closed = False
    def agent_arguments(self, journal, *, stop_event):
        return [str(self.root / 'runtime/python-hardware.exe'), '-B', '-I', '-m',
                'panasonic_posture.windows.session.agent', '--journal', str(journal), '--stop-event', stop_event]


class Api:
    def __init__(self):
        self.events = []
        self.user = USER
        self.peer = PEER
        self.elevated = False
        self.active = True
        self.running = True
        self.fail_assign = self.fail_start = False
    def require_system(self): self.events.append('system')
    def console_token(self): self.events.append('token'); return (10, self.user) if self.user else (None, None)
    def duplicate(self, token): self.events.append('duplicate'); return 11
    def environment(self, token): self.events.append('environment'); return 12
    def destroy_environment(self, environment): self.events.append('environment-close')
    def journal(self, token): return Path('C:/Users/fixture/AppData/Local/PanasonicPostureManager/native-display.json')
    def job(self): self.events.append('job'); return 13
    def start(self, token, arguments, environment, directory):
        self.events.append(('start', token, arguments, environment, directory))
        if self.fail_start: raise OSError('fixture creation failed')
        return 14, 15, PEER.process_id
    def assign(self, job, process):
        self.events.append('assign')
        if self.fail_assign: raise OSError('fixture job assignment failed')
    def identity(self, pid): self.events.append('identity'); return self.peer
    def process_elevated(self, process): return self.elevated
    def is_active(self, peer): return self.active
    def resume(self, thread): self.events.append('resume')
    def alive(self, process): return self.running
    def terminate(self, process): self.events.append('terminate'); self.running = False
    def wait(self, process, timeout): self.events.append(('wait', timeout)); return not self.running
    def close(self, handle): self.events.append(('close', handle))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.api = Api()
        self.runtime = Runtime()
        self.launcher = NativeSessionLauncher(self.runtime, api=self.api,
            signal_factory=lambda sid: Signal(self.api.events))
        self.addCleanup(self.launcher.close)

    def test_process_is_created_suspended_and_cannot_run_before_explicit_resume(self):
        self.assertEqual(self.launcher.probe(), USER)
        child = self.launcher.create(USER)
        self.assertEqual(child.identity, PEER)
        self.assertNotIn('resume', self.api.events)
        self.assertIn('assign', self.api.events)
        self.assertIn(('close', 10), self.api.events)
        self.assertIn(('close', 11), self.api.events)
        self.assertIn('environment-close', self.api.events)
        creation = next(event for event in self.api.events if isinstance(event, tuple) and event[0] == 'start')
        self.assertEqual(creation[2][-2:], ['--stop-event', Signal.name])
        child.resume()
        self.assertIn('resume', self.api.events)
        self.assertGreater(self.api.events.index('resume'), self.api.events.index('identity'))
        with self.assertRaises(RuntimeError): child.resume()
        child.request_stop()
        self.assertIn('stop', self.api.events)
        child.close()
        self.assertIn('terminate', self.api.events)
        self.assertIn(('close', 14), self.api.events)
        self.assertIn(('close', 13), self.api.events)
        self.assertIn('signal-close', self.api.events)

    def test_locked_switched_or_missing_console_never_creates_a_process(self):
        for user in (None, ConsoleUser(USER.sid, USER.logon_id, 2, True),
                     ConsoleUser(USER.sid, USER.logon_id, 1, False)):
            self.api.user = user
            with self.assertRaises((OSError, ValueError)): self.launcher.create(USER)
            self.assertFalse(any(isinstance(event, tuple) and event[0] == 'start' for event in self.api.events))

    def test_identity_mismatch_elevation_or_lost_console_kills_before_resume(self):
        for change in ('identity', 'elevation', 'active'):
            with self.subTest(change=change):
                api = Api()
                if change == 'identity': api.peer = ConsoleIdentity(USER.sid, 101, 1, 42, 12345)
                if change == 'elevation': api.elevated = True
                if change == 'active': api.active = False
                launcher = NativeSessionLauncher(self.runtime, api=api, signal_factory=lambda sid: Signal(api.events))
                with self.assertRaises(PermissionError): launcher.create(USER)
                self.assertIn('terminate', api.events)
                self.assertNotIn('resume', api.events)
                launcher.close()

    def test_job_or_creation_failure_closes_every_owned_resource(self):
        for stage in ('fail_start', 'fail_assign'):
            api = Api()
            setattr(api, stage, True)
            launcher = NativeSessionLauncher(self.runtime, api=api, signal_factory=lambda sid: Signal(api.events))
            with self.assertRaises(OSError): launcher.create(USER)
            self.assertIn(('close', 10), api.events)
            self.assertIn(('close', 11), api.events)
            self.assertIn('environment-close', api.events)
            self.assertIn(('close', 13), api.events)
            self.assertIn('signal-close', api.events)
            if stage == 'fail_assign': self.assertIn('terminate', api.events)
            self.assertNotIn('resume', api.events)
            launcher.close()

    def test_only_one_child_and_a_live_runtime_may_be_used(self):
        child = self.launcher.create(USER)
        with self.assertRaises(OSError): self.launcher.create(USER)
        self.runtime.closed = True
        with self.assertRaises(OSError): child.resume()
        child.close()
        with self.assertRaises(OSError): self.launcher.create(USER)

    def test_qualification_is_rechecked_immediately_before_resume(self):
        child = self.launcher.create(USER)
        self.api.active = False
        with self.assertRaises(PermissionError): child.resume()
        self.assertNotIn('resume', self.api.events)

    def test_stop_before_or_during_qualification_prevents_resume(self):
        child = self.launcher.create(USER)
        child.request_stop()
        with self.assertRaises(OSError): child.resume()
        self.assertNotIn('resume', self.api.events)
        child.signal.signaled = False
        def stopped(peer):
            child.request_stop()
            return True
        self.api.is_active = stopped
        with self.assertRaises(OSError): child.resume()
        self.assertNotIn('resume', self.api.events)

    def test_failed_temporary_handle_cleanup_prevents_opening_more_tokens(self):
        close = self.api.close
        def failed(handle):
            if handle == 10: raise OSError('fixture token close failed')
            close(handle)
        self.api.close = failed
        with self.assertRaises(OSError): self.launcher.probe()
        tokens = self.api.events.count('token')
        self.api.close = close
        with self.assertRaises(OSError): self.launcher.probe()
        with self.assertRaises(OSError): self.launcher.create(USER)
        self.assertEqual(self.api.events.count('token'), tokens)

    def test_failed_cleanup_retains_the_child_and_blocks_replacement_until_retried(self):
        child = self.launcher.create(USER)
        close = self.api.close
        def fail_job(handle):
            if handle == 13: raise OSError('fixture cannot close job yet')
            close(handle)
        self.api.close = fail_job
        with self.assertRaises(OSError): child.close()
        self.assertFalse(child.closed)
        with self.assertRaises(OSError): self.launcher.create(USER)
        self.api.close = close
        child.close()
        self.assertTrue(child.closed)


class NativeAPITests(unittest.TestCase):
    def api(self):
        api = object.__new__(NativeLaunchAPI)
        api.security = SimpleNamespace(_check=lambda value: value if value else (_ for _ in ()).throw(OSError('fixture API failure')))
        return api

    def test_windows_layout_and_creation_flags_do_not_depend_on_host_long_size(self):
        self.assertEqual(C.sizeof(StartupInfo), 104)
        self.assertEqual(C.sizeof(ProcessInfo), 24)
        self.assertEqual(C.sizeof(ExtendedLimits), 144)
        self.assertEqual(CREATE_FLAGS, 0x08000000 | 0x400 | 4)

    def test_primary_token_rights_cover_process_environment_and_known_folder_queries(self):
        api, seen = self.api(), []
        def duplicate(token, rights, attributes, level, kind, target):
            seen.append((rights, attributes, level, kind))
            target._obj.value = 11
            return True
        api.advapi = SimpleNamespace(DuplicateTokenEx=duplicate)
        self.assertEqual(api.duplicate(10), 11)
        self.assertEqual(seen, [(1 | 2 | 4 | 8, None, 2, 1)])

    def test_native_creation_has_no_inherited_handles_and_an_explicit_hidden_user_desktop(self):
        api, seen = self.api(), []
        def create(token, program, command, process_attrs, thread_attrs, inherit, flags, environment, directory, startup, target):
            self.assertFalse(inherit)
            self.assertEqual(flags, CREATE_FLAGS)
            self.assertEqual(startup._obj.desktop, r'winsta0\default')
            self.assertEqual(startup._obj.show, 0)
            self.assertIsNone(startup._obj.stdin)
            self.assertEqual((process_attrs, thread_attrs), (None, None))
            seen.append((program, command.value, environment, directory))
            target._obj.process, target._obj.thread, target._obj.pid = 14, 15, 42
            return True
        api.advapi = SimpleNamespace(CreateProcessAsUserW=create)
        args = [r'C:\Program Files\Posture\runtime\python-hardware.exe', '-B', '-I', '-m',
                'panasonic_posture.windows.session.agent']
        result = api.start(11, args, 12, Path('C:/Program Files/Posture'))
        self.assertEqual(result, (14, 15, 42))
        self.assertEqual(seen[0][0], args[0])
        self.assertTrue(seen[0][1].startswith(
            '"C:\\Program Files\\Posture\\runtime\\python-hardware.exe" '
        ))
        self.assertEqual(seen[0][2], 12)

    def test_job_kills_on_last_handle_close_without_a_named_or_inherited_handle(self):
        api, calls = self.api(), []
        def create(attributes, name):
            self.assertIsNone(attributes)
            self.assertIsNone(name)
            return 13
        def limits(job, kind, value, size):
            calls.append((job, kind, value._obj.basic.flags, size))
            return True
        api.kernel = SimpleNamespace(CreateJobObjectW=create, SetInformationJobObject=limits)
        self.assertEqual(api.job(), 13)
        self.assertEqual(calls, [(13, 9, 0x2000, 144)])

    def test_incomplete_native_creation_still_closes_process_if_thread_close_fails(self):
        api, closed, terminated = self.api(), [], []
        def create(*arguments):
            target = arguments[-1]._obj
            target.process, target.thread, target.pid = 14, 15, 0
            return True
        api.advapi = SimpleNamespace(CreateProcessAsUserW=create)
        api.alive = lambda process: True
        api.terminate = lambda process: terminated.append(process)
        api.wait = lambda process, timeout: True
        def close(handle):
            closed.append(handle)
            if handle == 15: raise OSError('fixture thread close failed')
        api.close = close
        with self.assertRaises(OSError): api.start(11, ['fixed-program'], 12, Path('fixed-directory'))
        self.assertEqual(terminated, [14])
        self.assertEqual(closed, [15, 14])

    def console_api(self, *, elevated=False, linked_elevated=False, linked_logon=100, flags=1):
        api = self.api()
        closed, freed = [], []
        info = ExtendedInfo()
        info.level = 1
        info.data.session, info.data.state, info.data.flags = 1, 0, flags
        def query_token(session, target):
            target._obj.value = 10
            return True
        def query_info(server, session, kind, target, size):
            target._obj.value = C.addressof(info)
            size._obj.value = C.sizeof(info)
            return True
        def identity(token):
            handle = token.value if isinstance(token, HANDLE) else token
            return USER.sid, linked_logon if handle == 11 else USER.logon_id, USER.session_id
        def token_info(token, kind):
            handle = token.value if isinstance(token, HANDLE) else token
            if kind == 19:
                return C.create_string_buffer((11).to_bytes(C.sizeof(HANDLE), 'little'), C.sizeof(HANDLE))
            value = linked_elevated if handle == 11 else elevated
            return C.create_string_buffer(int(value).to_bytes(4, 'little'), 4)
        api.kernel = SimpleNamespace(WTSGetActiveConsoleSessionId=lambda: 1,
            CloseHandle=lambda handle: closed.append(handle) or True)
        api.wts = SimpleNamespace(WTSQueryUserToken=query_token, WTSQuerySessionInformationW=query_info,
                                  WTSFreeMemory=lambda buffer: freed.append(buffer.value))
        api.security.token_identity, api.security._token_info = identity, token_info
        return api, closed, freed

    def test_elevated_console_token_uses_only_same_logon_non_elevated_linked_token(self):
        api, closed, freed = self.console_api(elevated=True)
        token, user = api.console_token()
        self.assertEqual((token, user), (11, USER))
        self.assertEqual(closed, [10])
        self.assertEqual(len(freed), 1)
        api.close(token)
        for kwargs in ({'linked_elevated': True}, {'linked_logon': 101}):
            api, closed, _ = self.console_api(elevated=True, **kwargs)
            with self.assertRaises(PermissionError): api.console_token()
            self.assertEqual(closed, [11, 10])

    def test_unknown_console_flags_close_token_and_wts_buffer_without_qualification(self):
        api, closed, freed = self.console_api(flags=-1)
        with self.assertRaises(PermissionError): api.console_token()
        self.assertEqual(closed, [10])
        self.assertEqual(len(freed), 1)
