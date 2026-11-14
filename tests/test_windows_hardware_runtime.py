"""Hardware-service composition: ordering, physical gating and cleanup."""
import time
import threading
import unittest
from unittest.mock import patch

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.service.runtime import HardwareRuntime, RESUME_GAP_SECONDS
from panasonic_posture.windows.service.launch import NativeSessionLauncher


class Switch:
    def __init__(self, events, value=False, status=HardwareStatus.OK):
        self.events = events
        self.value = value
        self.status = status
        self.closed = False

    def start(self):
        self.events.append('switch-start')

    def query(self):
        return self.value, self.status

    def bootstrap(self):
        self.events.append('switch-bootstrap')

    def finalize_bootstrap(self, fallback):
        self.events.append(('switch-finalize', fallback))

    def close(self):
        self.closed = True
        self.events.append('switch-close')


class Sensor:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def start(self):
        self.events.append('sensor-start')

    def close(self):
        self.closed = True
        self.events.append('sensor-close')


class Engine:
    def __init__(self, events):
        self.events = events
        self.ticks = 0
        self.resets = 0
        self.shutdowns = 0

    def start(self):
        self.events.append('engine-start')

    def shutdown(self):
        self.shutdowns += 1
        self.events.append('engine-shutdown')

    def tick(self):
        self.ticks += 1
        self.events.append('engine-tick')

    def reset_after_session_context_loss(self):
        self.resets += 1
        self.events.append('engine-reset')


class Runtime:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def close(self):
        self.closed = True
        self.events.append('runtime-close')


class Launcher:
    def __init__(self, events):
        self.events = events
        self.closed = False

    def close(self):
        self.closed = True
        self.events.append('launcher-close')


class Server:
    def __init__(self, events, engine, admission):
        self.events, self.engine, self.admission = events, engine, admission
        self.registered = []
        self.closed = False

    def register_session_process(self, peer):
        self.events.append(('register', peer))
        self.registered.append(peer)

    def poll(self):
        self.events.append('server-poll')
        self.engine.tick()

    def close(self):
        self.closed = True
        self.events.append('server-close')


class Supervisor:
    def __init__(self, events):
        self.events = events
        self.closed = False
        self.requests = 0

    def poll(self):
        self.events.append('supervisor-poll')

    def request_stop(self):
        self.requests += 1
        self.closed = True
        self.events.append('supervisor-stop')

    def close(self):
        self.events.append('supervisor-close')


class HardwareRuntimeTests(unittest.TestCase):
    def test_stop_deadline_does_not_wait_for_the_launcher_lock_or_unpin_live_work(self):
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        events, result = [], {}

        class API:
            def require_system(self):
                pass
            def console_token(self):
                entered.set()
                release.wait()
                return None, None

        def run():
            engine, trusted = Engine(events), Runtime(events)
            launcher = NativeSessionLauncher(trusted, api=API())
            runtime = HardwareRuntime(
                engine=engine, switch=Switch(events), sensor=Sensor(events), runtime=trusted,
                server_factory=lambda admission: Server(events, engine, admission),
                launcher_factory=lambda: launcher, stop_timeout=.02,
            )
            result.update(runtime=runtime, trusted=trusted, launcher=launcher)
            try:
                runtime.start()
                runtime.poll()
                entered.wait(2)
                runtime.close()
            except OSError as error:
                result['error'] = error
            finally:
                stopped.set()

        caller = threading.Thread(target=run)
        caller.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertTrue(stopped.wait(1), 'service stop waited for a blocked native query')
            self.assertIn('error', result)
            self.assertFalse(result['trusted'].closed)
            self.assertIn('server-close', events)
            self.assertIn('switch-close', events)
        finally:
            release.set()
            caller.join(2)
        self.assertFalse(caller.is_alive())
        result['runtime']._cleanup_task.result(timeout=2)
        self.assertTrue(result['launcher'].closed)
        self.assertTrue(result['trusted'].closed)

    def make(self, *, value=False, status=HardwareStatus.OK, clock=time.monotonic):
        events = []
        switch, sensor, engine, runtime = Switch(events, value, status), Sensor(events), Engine(events), Runtime(events)

        def server_factory(admission):
            events.append('server-create')
            return Server(events, engine, admission)

        def launcher_factory():
            events.append('launcher-create')
            return Launcher(events)

        def supervisor_factory(launcher, register, *, clock):
            del launcher, register, clock
            events.append('supervisor-create')
            return Supervisor(events)

        runtime_object = HardwareRuntime(
            engine=engine,
            switch=switch,
            sensor=sensor,
            runtime=runtime,
            server_factory=server_factory,
            launcher_factory=launcher_factory,
            supervisor_factory=supervisor_factory,
            clock=clock,
        )
        return runtime_object, events, switch, engine, sensor, runtime

    def test_startup_starts_physical_source_before_sensor_then_rpc_and_supervisor(self):
        runtime, events, _, _, _, _ = self.make()
        runtime.start()
        self.assertEqual(events, [
            'switch-start', 'engine-start', 'server-create',
            'launcher-create', 'supervisor-create',
        ])
        runtime.close()

    def test_first_event_is_not_required_to_start_the_desktop_session_lifecycle(self):
        runtime, _, switch, _, _, _ = self.make(value=None, status=HardwareStatus.ABSENT)
        runtime.start()
        self.assertIsNotNone(runtime.supervisor)
        switch.value, switch.status = False, HardwareStatus.OK
        runtime.close()

    def test_bootstrap_finalization_uses_the_explicit_switch_contract(self):
        runtime, events, _, _, _, _ = self.make(value=None, status=HardwareStatus.ABSENT)
        runtime.start()
        runtime._bootstrap_until = 0.
        runtime.poll()
        self.assertIn(('switch-finalize', False), events)
        runtime.close()

    def test_physical_source_failure_is_read_only_and_recovery_reopens_admission(self):
        runtime, events, switch, _, _, _ = self.make(value=None, status=HardwareStatus.READ_ERROR)
        runtime.start()
        self.assertIsNotNone(runtime.server)
        self.assertIsNotNone(runtime.supervisor)
        self.assertFalse(runtime.admitted())
        switch.value, switch.status = True, HardwareStatus.OK
        self.assertTrue(runtime.admitted())
        switch.value, switch.status = None, HardwareStatus.READ_ERROR
        self.assertFalse(runtime.admitted())
        runtime.close()

    def test_poll_ticks_the_engine_once_through_the_server(self):
        runtime, events, _, engine, _, _ = self.make()
        runtime.start()
        runtime.poll()
        self.assertEqual(engine.ticks, 1)
        self.assertEqual(events[-3:], ['supervisor-poll', 'server-poll', 'engine-tick'])
        runtime.close()

    def test_sleep_gap_resets_old_posture_before_any_session_request(self):
        now = [10.]
        runtime, events, _, engine, _, _ = self.make(clock=lambda: now[0])
        runtime.start()
        events.clear()

        now[0] += RESUME_GAP_SECONDS
        runtime.poll()

        self.assertEqual(engine.resets, 1)
        self.assertEqual(events[0], 'engine-reset')
        self.assertLess(events.index('engine-reset'), events.index('server-poll'))
        self.assertLess(events.index('server-poll'), events.index('engine-tick'))
        runtime.poll()
        self.assertEqual(engine.resets, 1)
        runtime.close()

    def test_close_stops_supervisor_before_releasing_owned_resources(self):
        runtime, events, switch, engine, sensor, trusted_runtime = self.make()
        runtime.start()
        events.clear()
        runtime.close()
        self.assertIn('supervisor-stop', events)
        self.assertIn('supervisor-close', events)
        self.assertIn('server-close', events)
        self.assertEqual(engine.shutdowns, 1)
        self.assertTrue(switch.closed)
        self.assertTrue(sensor.closed)
        self.assertTrue(trusted_runtime.closed)

    def test_startup_failure_closes_every_owned_resource(self):
        runtime, events, switch, engine, sensor, trusted_runtime = self.make()

        def fail_start():
            raise OSError('fixture ETW startup failure')

        switch.start = fail_start
        with self.assertRaises(OSError):
            runtime.start()
        self.assertTrue(switch.closed)
        self.assertTrue(sensor.closed)
        self.assertTrue(trusted_runtime.closed)
        self.assertEqual(engine.shutdowns, 1)

    def test_installed_delegates_production_wiring_to_the_application_root(self):
        result = object()
        trusted_runtime = object()
        def clock():
            return 1.
        def sleep(_seconds):
            return None
        with patch(
            'panasonic_posture.windows.service.application.create_hardware_runtime',
            return_value=result,
        ) as create:
            actual = HardwareRuntime.installed(
                runtime=trusted_runtime,
                settings='settings',
                device='device',
                clock=clock,
                sleep=sleep,
                stop_timeout=3,
            )
        self.assertIs(actual, result)
        self.assertEqual(create.call_args.kwargs, dict(
            runtime=trusted_runtime,
            settings='settings',
            device='device',
            server_factory=None,
            launcher_factory=None,
            supervisor_factory=None,
            wake=None,
            clock=clock,
            sleep=sleep,
            stop_timeout=3,
        ))


if __name__ == '__main__':
    unittest.main()
