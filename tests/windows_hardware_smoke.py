"""Isolated Windows SYSTEM/user pipe test. Never opens or installs a driver.

Run server as LocalSystem and client as the normal interactive test user in
an explicitly authorized VM. This is a transport fixture with an unsupported
engine and inert input/sensor ports, not a device-acceptance script.
"""
import argparse
import ctypes
import json
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from panasonic_posture.controller import Controller
from panasonic_posture.model import DeviceInfo, SensorInfo, SensorReading, HardwareStatus
from panasonic_posture.presentation import PosePresentation
from panasonic_posture.service.engine import Engine, ServiceError
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.hardware_ipc import HardwareServer, HardwareConnection, NativePipes
from panasonic_posture.windows.security import WindowsSecurity, AuthenticationError

TEST_PIPE = r'\\.\pipe\PanasonicPostureManager.Hardware.Validation.v1'
ACL_PIPE = TEST_PIPE + '.acl'
USER_PIPE = TEST_PIPE + '.untrusted'


def server(directory):
    security = WindowsSecurity()
    security.require_system()
    sensor = SimpleNamespace(info=SensorInfo(), reading=lambda: SensorReading())
    actuator = SimpleNamespace(release=lambda: None)
    engine = Engine(controller=Controller(), sensor=sensor, actuator=actuator,
        presentation=PosePresentation(), settings=None,
        device=DeviceInfo(platform='windows-validation', support_tier='unsupported'),
        hardware_query=lambda: (None, HardwareStatus.ABSENT), executor=InlineExecutor())
    native = NativePipes(security)
    acl = native.create(ACL_PIPE, first=True)
    host = HardwareServer(engine, security=security, pipes=native, pipe_name=TEST_PIPE)
    try:
        (directory / 'server-ready.json').write_text(json.dumps({'ready': True}), encoding='utf-8')
        deadline = time.monotonic() + 120
        while not (directory / 'client-result.json').exists():
            if time.monotonic() >= deadline:
                raise TimeoutError('normal-user pipe test did not finish')
            host.poll()
            time.sleep(.01)
        return {'success': True, 'supported': engine.state.supported, 'applied': engine.state.applied}
    finally:
        host.close()
        native.close(acl)


def client(directory):
    native = NativePipes()
    security = native.security
    try:
        security.require_system()
    except AuthenticationError:
        pass
    else:
        raise AssertionError('fixture client must run as a normal user')
    deadline = time.monotonic() + 30
    while not (directory / 'server-ready.json').exists():
        if time.monotonic() >= deadline:
            raise TimeoutError('SYSTEM pipe server did not start')
        time.sleep(.05)
    connection = HardwareConnection(pipe_name=TEST_PIPE)
    try:
        reply = connection.request('get_state', {})
        assert reply['state']['supported'] is False and reply['state']['applied'] is None
        try:
            connection.request('set_mode', {'mode': 'tent'})
        except ServiceError as error:
            assert error.code == 'unsupported-model'
        else:
            raise AssertionError('unsupported hardware accepted a mode')
    finally:
        connection.close()
    connection = HardwareConnection(pipe_name=TEST_PIPE)
    try:
        reply = connection.request('get_state', {})
        try:
            connection.request('session_update', dict(generation=reply['state']['generation'],
                ready=True, error_code=None, error_detail=None, unattended=False))
        except ServiceError as error:
            assert error.code == 'access-denied'
        else:
            raise AssertionError('unregistered process submitted a display receipt')
    finally:
        connection.close()
    # The client ACE must not grant FILE_CREATE_PIPE_INSTANCE. The SYSTEM
    # fixture holds only one of this name's four possible instances.
    handle = native.kernel.CreateNamedPipeW(ACL_PIPE, 3, 4 | 2 | 1 | 8, 4, 65536, 65536, 0, None)
    if handle != ctypes.c_void_p(-1).value:
        native.close(handle)
        raise AssertionError('normal client created a privileged pipe instance')
    assert ctypes.get_last_error() == 5
    # A user-owned pipe with a plausible name cannot impersonate the service.
    handle = native.kernel.CreateNamedPipeW(USER_PIPE, 3 | 0x80000, 4 | 2 | 1 | 8, 1, 65536, 65536, 0, None)
    assert handle != ctypes.c_void_p(-1).value
    try:
        try:
            native.connect(USER_PIPE)
        except AuthenticationError:
            pass
        else:
            raise AssertionError('client trusted a non-SYSTEM pipe owner')
    finally:
        native.close(handle)
    return {'success': True, 'elevated': bool(ctypes.windll.shell32.IsUserAnAdmin()),
            'checks': ['system-owner', 'console-token', 'unsupported-mode',
                       'unregistered-receipt', 'no-client-server-instance', 'reject-user-owned-pipe']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('role', choices=('server', 'client'))
    parser.add_argument('--directory', type=Path, required=True)
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('this isolated fixture requires Windows')
    options.directory.mkdir(parents=True, exist_ok=True)
    result = None
    try:
        result = globals()[options.role](options.directory)
    except BaseException as error:
        result = {'success': False, 'error': str(error), 'traceback': traceback.format_exc()}
    finally:
        (options.directory / (options.role + '-result.json')).write_text(
            json.dumps(result, indent=2), encoding='utf-8')
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
