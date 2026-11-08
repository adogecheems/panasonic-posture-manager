"""Exercise the Python actuator against compiled control.c, never a device.

Pass the user-mode DLL emitted by build-driver.py --control-fixture. This
verifies binary receipts and C watchdog decisions; it is not kernel or input
hardware validation and requires no elevation or boot security changes.
"""
import argparse
import ctypes as C
from pathlib import Path
from types import SimpleNamespace

from panasonic_posture.model import Target
from panasonic_posture.service.ports import ActuatorError
from panasonic_posture.windows.actuator import WindowsActuator, REQUEST, REPLY


class Request(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ('version', 'size', 'operation', 'reserved')] + [
        (name, C.c_uint64) for name in ('serial', 'low', 'high')]


class Reply(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ('version', 'size', 'status', 'flags')] + [
        (name, C.c_uint64) for name in ('serial', 'low', 'high')] + [
        (name, C.c_uint32) for name in ('ready', 'remaining', 'busy', 'reserved')]


class Connection(C.Structure):
    _fields_ = [(name, C.c_uint64) for name in ('serial', 'low', 'high')]


class Guard(C.Structure):
    _fields_ = [('deadline', C.c_uint64), ('ready', C.c_uint32), ('busy', C.c_uint32), ('blocked', C.c_int)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--control-library', type=Path, required=True)
    options = parser.parse_args()
    library = C.CDLL(str(options.control_library.resolve()))
    library.posture_command.argtypes = [C.POINTER(Guard), C.POINTER(Connection),
                                       C.POINTER(Request), C.c_uint64, C.POINTER(Reply)]
    library.posture_command.restype = None
    library.posture_release.argtypes = [C.POINTER(Guard)]
    library.posture_release.restype = None
    assert (C.sizeof(Request), C.sizeof(Reply)) == (REQUEST.size, REPLY.size)
    now = [10.]
    guard = Guard(0, 3, 0, 0)

    class Transport:
        def __init__(self):
            self.connection = Connection()
        def exchange(self, packet):
            request, reply = Request.from_buffer_copy(packet), Reply()
            library.posture_command(C.byref(guard), C.byref(self.connection), C.byref(request),
                                    int(now[0] * 1000), C.byref(reply))
            return bytes(reply)
        def close(self):
            library.posture_release(C.byref(guard))

    actuator = WindowsActuator(factory=Transport, clock=lambda: now[0])
    target = Target(mode='tent', tablet=True, rotation=180, disable_internal_input=True)
    try:
        actuator.keep(now[0])
        assert guard.blocked and actuator.state.satisfies(target)
        now[0] += 1
        actuator.keep(now[0])
        assert guard.deadline == 14000
        now[0] += 4
        assert not actuator.state.satisfies(target)
        try:
            actuator.keep(now[0])
        except ActuatorError:
            pass
        else:
            raise AssertionError('expired C lease was silently reacquired')
        assert not guard.blocked and not actuator.running
        actuator.retry()
        guard.busy = 1
        actuator.keep(now[0])
        assert not guard.blocked and not actuator.state.satisfies(target)
        guard.busy = 0
        actuator.keep(now[0])
        assert guard.blocked and actuator.state.satisfies(target)
        actuator.release()
        assert not guard.blocked and not actuator.running
    finally:
        actuator.release()
    # Verify the same C-backed actuator through the real service engine. A
    # ready input driver alone cannot bypass the authenticated display receipt.
    from panasonic_posture.controller import Controller
    from panasonic_posture.model import DeviceInfo, HardwareStatus, SensorInfo, SensorReading
    from panasonic_posture.presentation import PosePresentation
    from panasonic_posture.service.engine import Engine
    from panasonic_posture.service.ports import SessionOwner
    from panasonic_posture.tasks import InlineExecutor
    engine = Engine(controller=Controller(), presentation=PosePresentation(),
        sensor=SimpleNamespace(info=SensorInfo(ready=True, backend='windows-fixture'),
            reading=lambda: SensorReading((0., 0., -9.81), now[0], 'windows-fixture')),
        actuator=actuator, settings=None,
        device=DeviceInfo(platform='windows-fixture', support_tier='compatible'),
        hardware_query=lambda: (False, HardwareStatus.OK),
        monotonic=lambda: now[0], executor=InlineExecutor())
    owner = SessionOwner('windows-fixture', 'registered-agent')
    try:
        for _ in range(80):
            now[0] += .05
            engine.tick()
        engine.set_mode('tent')
        engine.tick()
        generation = engine.state.generation
        engine.session_update(generation, False, None, None, owner=owner)
        engine.tick()
        assert not guard.blocked and engine.state.applied is None
        assert not engine.session_update(generation + 1, True, None, None, owner=owner)
        assert not guard.blocked and engine.state.applied is None
        engine.session_update(generation, True, None, None, owner=owner)
        engine.tick()
        assert guard.blocked and engine.state.applied is not None and engine.state.applied.mode == 'tent', (
            engine.state.to_dict(), actuator.error_code, actuator.error_detail)
        now[0] += 4
        engine.tick()
        assert not guard.blocked and engine.state.applied is None
    finally:
        engine.shutdown()
    print('Python/C ABI, acquisition, renewal, expiry, busy admission and release passed; no device accessed')
    print('Shared engine: matching authenticated display receipt required; lease loss revoked input')


if __name__ == '__main__':
    main()
