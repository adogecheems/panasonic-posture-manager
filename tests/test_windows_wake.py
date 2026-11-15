"""Wake-event publication: signal-only transport and runtime change detection."""
import unittest
import ctypes as C
import sys
from uuid import uuid4

from panasonic_posture.windows.common.security import HANDLE, I32

from panasonic_posture.model import (
    BackendState,
    HardwareStatus,
    Mode,
    Pose,
    StateSnapshot,
    Target,
)
from panasonic_posture.windows.ipc.wake import (
    WAIT_OBJECT_0,
    WAIT_TIMEOUT,
    WakeEvent,
)
from panasonic_posture.windows.service.runtime import (
    HardwareRuntime,
    _state_signature,
)


def make_state():
    return StateSnapshot(
        instance='test',
        hardware=False,
        hardware_status=HardwareStatus.OK,
        manual=Mode.AUTO,
        pose=Pose.LAPTOP,
        target=Target(),
        applied=None,
        backend=BackendState.READY,
    )


class WakeEventTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'native event ACL requires Windows')
    def test_create_uses_a_null_dacl_instead_of_the_service_token_default(self):
        event = WakeEvent(name=f'PanasonicPostureManager.Wake.Test.{uuid4().hex}')
        event.create()
        self.addCleanup(event.close)
        descriptor, dacl = HANDLE(), HANDLE()
        api = event.security.advapi
        event.security._fn(api, 'GetSecurityDescriptorDacl', I32, HANDLE,
                           C.POINTER(I32), C.POINTER(HANDLE), C.POINTER(I32))
        error = api.GetSecurityInfo(event.handle, 6, 4, None, None, C.byref(dacl), None,
                                    C.byref(descriptor))
        self.assertEqual(error, 0)
        try:
            present, defaulted = I32(), I32()
            self.assertTrue(api.GetSecurityDescriptorDacl(
                descriptor, C.byref(present), C.byref(dacl), C.byref(defaulted)))
            self.assertTrue(present.value)
            self.assertIsNone(dacl.value)
        finally:
            event.kernel.LocalFree(descriptor)

    def test_signature_only_changes_with_observable_fields(self):
        state = make_state()
        before = _state_signature(state)
        self.assertEqual(_state_signature(state), before)
        state.generation += 1
        self.assertNotEqual(_state_signature(state), before)

    @unittest.skipUnless(sys.platform == 'win32', 'native named events require Windows')
    def test_named_event_roundtrip_auto_resets(self):
        # An unqualified name lands in the session-local namespace, so the
        # unprivileged test process can create it while the production name
        # uses Global\ for cross-session visibility.
        publisher = WakeEvent(name='PanasonicPostureManager.Wake.Test')
        publisher.create()
        self.addCleanup(publisher.close)
        watcher = WakeEvent(name='PanasonicPostureManager.Wake.Test')
        watcher.open()
        self.addCleanup(watcher.close)
        self.assertEqual(watcher.wait(0), WAIT_TIMEOUT)
        publisher.publish()
        self.assertEqual(watcher.wait(1000), WAIT_OBJECT_0)
        # The auto-reset event is consumed by one waiter.
        self.assertEqual(watcher.wait(0), WAIT_TIMEOUT)

    @unittest.skipUnless(sys.platform == 'win32', 'native named events require Windows')
    def test_closed_event_rejects_operations(self):
        event = WakeEvent(name='PanasonicPostureManager.Wake.Test.Closed')
        with self.assertRaises(OSError):
            event.publish()
        with self.assertRaises(OSError):
            event.wait(0)


class Wake:
    def __init__(self):
        self.published = 0
        self.created = False
        self.closed = False

    def create(self):
        self.created = True

    def publish(self):
        self.published += 1

    def close(self):
        self.closed = True


class Switch:
    def start(self): ...
    def query(self):
        return False, HardwareStatus.OK
    def bootstrap(self): ...
    def finalize_bootstrap(self, fallback): ...
    def close(self): ...


class Sensor:
    def start(self): ...
    def close(self): ...


class RuntimePort:
    def close(self): ...


class EnginePort:
    def __init__(self):
        self.state = make_state()

    def start(self): ...
    def shutdown(self): ...
    def tick(self): ...
    def reset_after_session_context_loss(self): ...


class Server:
    def __init__(self, engine):
        self.engine = engine

    def register_session_process(self, peer): ...
    def poll(self): ...
    def close(self): ...


class Launcher:
    def close(self): ...


class Supervisor:
    def __init__(self):
        self.closed = False

    def poll(self): ...
    def request_stop(self):
        self.closed = True
    def close(self): ...


class RuntimeWakeTests(unittest.TestCase):
    def make(self):
        engine = EnginePort()
        wake = Wake()
        server = Server(engine)

        def server_factory(admission):
            del admission
            return server

        def launcher_factory():
            return Launcher()

        def supervisor_factory(launcher, register, *, clock):
            del launcher, register, clock
            return Supervisor()

        runtime = HardwareRuntime(
            engine=engine,
            switch=Switch(),
            sensor=Sensor(),
            runtime=RuntimePort(),
            server_factory=server_factory,
            launcher_factory=launcher_factory,
            supervisor_factory=supervisor_factory,
            wake=wake,
            power_factory=lambda callback: Sensor(),
        )
        return runtime, engine, wake

    def test_publishes_once_per_state_change_and_closes(self):
        runtime, engine, wake = self.make()
        runtime.start()
        self.assertTrue(wake.created)
        runtime.poll()
        self.assertEqual(wake.published, 1)
        runtime.poll()
        self.assertEqual(wake.published, 1)
        engine.state.generation += 1
        runtime.poll()
        self.assertEqual(wake.published, 2)
        runtime.close()
        self.assertTrue(wake.closed)


if __name__ == '__main__':
    unittest.main()
