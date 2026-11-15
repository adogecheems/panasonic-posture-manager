"""Host lifecycle: register before resume, revoke before stop, one work slot."""
from concurrent.futures import Future
import unittest

from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.service.host import ConsoleIdentity
from panasonic_posture.windows.service.launch import ConsoleUser
from panasonic_posture.windows.service.supervisor import SessionSupervisor

USER = ConsoleUser('S-1-5-21-1000', 100, 1, True)


class Child:
    def __init__(self, events, user, index):
        self.events = events
        self.identity = ConsoleIdentity(user.sid, user.logon_id, user.session_id, 42 + index, 12345 + index)
        self.running = True
        self.resumed = self.closed = False
        self.cooperative = True
    def alive(self): return self.running
    def successful_exit(self): return not self.running
    def resume(self): self.resumed = True; self.events.append(('resume', self.identity))
    def request_stop(self):
        self.events.append(('stop', self.identity))
        if self.cooperative: self.running = False
    def close(self):
        self.events.append(('close', self.identity))
        self.closed, self.running = True, False


class Launcher:
    def __init__(self, events):
        self.events = events
        self.user = USER
        self.children = []
    def probe(self): return self.user
    def create(self, user):
        child = Child(self.events, user, len(self.children))
        self.children.append(child)
        self.events.append(('create', child.identity))
        return child
    def close(self): self.events.append(('launcher-close', None))


class ManualExecutor:
    def __init__(self): self.jobs = []
    def submit(self, function):
        future = Future()
        self.jobs.append((function, future))
        return future
    def complete(self):
        for function, future in self.jobs:
            if not future.done():
                try: future.set_result(function())
                except Exception as error: future.set_exception(error)
                return


class SupervisorTests(unittest.TestCase):
    def test_stop_reports_failed_restoration_instead_of_success(self):
        supervisor = self.start()
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        peer, child = supervisor.child.identity, supervisor.child
        child.successful_exit = lambda: False
        self.assertFalse(supervisor.set_control(peer, False))
        self.until(lambda: supervisor.child is None)
        with self.assertRaisesRegex(OSError, 'restoration'):
            supervisor.set_control(peer, False)
        self.assertTrue(supervisor.set_control(peer, True))

    def test_stop_is_scoped_to_the_logon_and_does_not_block_a_different_user(self):
        supervisor = self.start()
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        peer = supervisor.child.identity
        supervisor.set_control(peer, False)
        self.until(lambda: supervisor.child is None)
        self.launcher.user = ConsoleUser(USER.sid, USER.logon_id + 1, USER.session_id, True)
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        self.assertEqual(supervisor.user.logon_id, USER.logon_id + 1)

    def test_explicit_stop_reaps_child_and_prevents_respawn_until_start(self):
        supervisor = self.start()
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        peer, child = supervisor.child.identity, supervisor.child
        self.assertFalse(supervisor.set_control(peer, False))
        self.until(lambda: supervisor.set_control(peer, False))
        self.assertTrue(child.closed)
        for _ in range(30):
            self.step()
        self.assertEqual(len(self.launcher.children), 1)
        self.assertTrue(supervisor.set_control(peer, True))
        self.until(lambda: len(self.launcher.children) == 2 and supervisor.child is not None and supervisor.child.resumed)

    def test_stop_during_pending_creation_never_resumes_the_late_child(self):
        self.worker = ManualExecutor()
        supervisor = self.start()
        self.step()
        self.worker.complete()
        self.step()
        peer = ConsoleIdentity(USER.sid, USER.logon_id, USER.session_id, 99, 123)
        self.assertFalse(supervisor.set_control(peer, False))
        self.until(lambda: supervisor.set_control(peer, False))
        child = self.launcher.children[0]
        self.assertTrue(child.closed)
        self.assertFalse(child.resumed)

    def setUp(self):
        self.now, self.events = 10., []
        self.launcher = Launcher(self.events)
        self.worker = InlineExecutor()
        self.supervisor = None

    def start(self):
        self.supervisor = SessionSupervisor(self.launcher,
            lambda peer: self.events.append(('register', peer)), clock=lambda: self.now, executor=self.worker)
        self.addCleanup(self.shutdown)
        return self.supervisor

    def step(self):
        self.now += .1
        self.supervisor.poll()

    def until(self, predicate):
        for _ in range(100):
            if isinstance(self.worker, ManualExecutor): self.worker.complete()
            self.step()
            if predicate(): return
        self.fail('session supervisor did not reach expected state')

    def shutdown(self):
        self.supervisor.request_stop()
        self.until(lambda: self.supervisor.closed)
        self.supervisor.close()

    def test_registration_precedes_resume_and_revocation_precedes_normal_stop(self):
        supervisor = self.start()
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        peer = supervisor.child.identity
        self.assertLess(self.events.index(('register', peer)), self.events.index(('resume', peer)))
        supervisor.request_stop()
        self.until(lambda: supervisor.closed)
        self.assertLess(self.events.index(('register', None)), self.events.index(('stop', peer)))
        self.assertLess(self.events.index(('stop', peer)), self.events.index(('close', peer)))

    def test_deferred_close_reaps_late_create_before_releasing_runtime_pins(self):
        self.worker = ManualExecutor()
        supervisor = SessionSupervisor(self.launcher, lambda peer: self.events.append(('register', peer)),
                                       clock=lambda: self.now, executor=self.worker)
        supervisor.poll()
        self.worker.complete()
        supervisor.poll()
        pending = supervisor.defer_close(lambda: self.events.append(('unpin', None)))
        self.assertFalse(pending.done())
        self.worker.complete()  # The create already in flight may finish late.
        child = self.launcher.children[0]
        self.assertFalse(child.resumed)
        self.worker.complete()  # Serialized cleanup runs after that create.
        pending.result()
        self.assertTrue(child.closed)
        self.assertTrue(supervisor.closed)
        self.assertLess(self.events.index(('close', child.identity)), self.events.index(('unpin', None)))
        self.assertNotIn(('register', child.identity), self.events)

    def test_failed_deferred_cleanup_keeps_the_runtime_pinned(self):
        self.worker = ManualExecutor()
        supervisor = SessionSupervisor(self.launcher, lambda peer: None,
                                       clock=lambda: self.now, executor=self.worker)
        self.launcher.close = lambda: (_ for _ in ()).throw(OSError('fixture close failed'))
        unpinned = []
        pending = supervisor.defer_close(lambda: unpinned.append(True))
        self.worker.complete()
        with self.assertRaises(OSError):
            pending.result()
        self.assertEqual(unpinned, [])
        self.assertFalse(supervisor.closed)

    def test_user_switch_reaps_previous_child_before_creating_replacement(self):
        supervisor = self.start()
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        old = supervisor.child
        self.launcher.user = ConsoleUser(USER.sid, 101, 2, True)
        self.until(lambda: len(self.launcher.children) == 2 and self.launcher.children[-1].resumed)
        new = supervisor.child
        self.assertTrue(old.closed)
        self.assertLess(self.events.index(('close', old.identity)), self.events.index(('create', new.identity)))

    def test_lock_keeps_same_process_but_does_not_launch_a_new_locked_session(self):
        supervisor = self.start()
        self.launcher.user = ConsoleUser(USER.sid, 100, 1, False)
        for _ in range(20): self.step()
        self.assertFalse(self.launcher.children)
        self.launcher.user = USER
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        child = supervisor.child
        self.launcher.user = ConsoleUser(USER.sid, 100, 1, False)
        for _ in range(20): self.step()
        self.assertIs(supervisor.child, child)
        self.assertFalse(child.closed)

    def test_failed_resume_revokes_and_closes_without_publishing_another_child_early(self):
        supervisor = self.start()
        original = self.launcher.create
        def create(user):
            child = original(user)
            def fail(): raise OSError('fixture qualification changed')
            child.resume = fail
            return child
        self.launcher.create = create
        self.until(lambda: bool(self.launcher.children) and self.launcher.children[0].closed)
        self.assertIn(('register', None), self.events)
        self.assertFalse(any(event[0] == 'resume' for event in self.events))

    def test_hung_probe_stays_one_job_and_stop_time_child_never_resumes(self):
        self.worker = ManualExecutor()
        supervisor = self.start()
        self.step()
        for _ in range(100): self.step()
        self.assertEqual(len(self.worker.jobs), 1)
        # Release the observed live handle, then hold the create operation.
        self.worker.complete()
        self.step()
        self.assertFalse(self.launcher.children)  # The expired observation cannot launch.
        self.until(lambda: len(self.worker.jobs) == 2)
        self.worker.complete()
        self.step()
        self.assertEqual(len(self.worker.jobs), 3)
        supervisor.request_stop()
        self.until(lambda: supervisor.closed)
        self.assertEqual(len(self.launcher.children), 1)
        self.assertTrue(self.launcher.children[0].closed)
        self.assertFalse(self.launcher.children[0].resumed)

    def test_unresponsive_child_gets_restore_grace_before_forced_cleanup(self):
        supervisor = self.start()
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        child = supervisor.child
        child.cooperative = False
        supervisor.request_stop()
        began = self.now
        while self.now - began < 4:
            self.step()
        self.assertFalse(child.closed)
        self.until(lambda: supervisor.closed)
        self.assertTrue(child.closed)

    def test_expired_create_result_is_reaped_without_registration_or_resume(self):
        self.worker = ManualExecutor()
        supervisor = self.start()
        self.step()
        self.worker.complete()
        self.step()  # A live create job now owns the one worker slot.
        self.now += 6
        self.worker.complete()
        self.step()
        child = self.launcher.children[0]
        self.assertFalse(child.resumed)
        self.assertNotIn(('register', child.identity), self.events)
        self.until(lambda: child.closed)

    def test_cleanup_failure_is_retried_before_any_replacement_can_start(self):
        supervisor = self.start()
        self.until(lambda: supervisor.child is not None and supervisor.child.resumed)
        old = supervisor.child
        close = old.close
        failed = []
        def retry_close():
            if not failed:
                failed.append(True)
                raise OSError('fixture close not complete yet')
            close()
        old.close = retry_close
        self.launcher.user = ConsoleUser(USER.sid, 101, 2, True)
        self.until(lambda: supervisor.error is not None)
        self.assertEqual(len(self.launcher.children), 1)
        self.until(lambda: len(self.launcher.children) == 2)
        self.assertTrue(old.closed)

    def test_physical_admission_does_not_own_the_desktop_session_lifecycle(self):
        supervisor = SessionSupervisor(self.launcher,
            lambda peer: self.events.append(('register', peer)),
            clock=lambda: self.now, executor=InlineExecutor())
        self.addCleanup(self.shutdown)
        self.supervisor = supervisor
        self.until(lambda: bool(self.launcher.children) and
                   self.launcher.children[0].resumed)
        self.assertTrue(supervisor.child.alive())
