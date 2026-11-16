"""Explicit SYSTEM/ordinary-user lifecycle fixture using a protected runtime.

The engine deliberately has no hardware support, sensor stream, input handle
or settings writer. Never use this to claim input or posture acceptance.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from types import SimpleNamespace

from panasonic_posture.controller import Controller
from panasonic_posture.model import DeviceInfo, HardwareStatus, SensorInfo, SensorReading
from panasonic_posture.presentation import PosePresentation
from panasonic_posture.service.engine import Engine
from panasonic_posture.windows.service.ipc import HardwareServer, HardwareConnection, HardwareOperationError
from panasonic_posture.windows.common.security import WindowsSecurity
from panasonic_posture.windows.service.launch import NativeSessionLauncher
from panasonic_posture.windows.service.supervisor import SessionSupervisor
from panasonic_posture.windows.service.trusted_runtime import TrustedRuntime


def prepare(directory: Path) -> dict:
    """Hash a minimal runtime snapshot; this does not elevate or execute it."""
    WindowsSecurity().require_unprivileged()
    repository = Path(__file__).resolve().parents[3]
    package = repository / 'src/panasonic_posture'
    if not package.is_dir(): raise ValueError('prepare from the source checkout')
    base = Path(sys.base_prefix)
    files = []
    def add(source, relative):
        with source.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        files.append(dict(source=str(source), relative=relative, hash=digest))
    for path in sorted(base.iterdir()):
        if path.is_file() and (path.suffix.lower() in ('.exe', '.dll') or path.name == 'LICENSE.txt'):
            add(path, 'runtime/' + path.name)
    for name in ('Lib', 'DLLs'):
        for path in sorted((base / name).rglob('*')):
            relative = path.relative_to(base)
            if (path.is_file() and path.suffix != '.pyc' and
                    not {'site-packages', '__pycache__'}.intersection(relative.parts)):
                add(path, 'runtime/' + relative.as_posix())
    for path in sorted(package.rglob('*.py')):
        if '__pycache__' not in path.parts:
            add(path, 'app/' + path.relative_to(repository / 'src').as_posix())
    add(Path(__file__).resolve(), 'fixture.py')
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory.resolve() / 'manifest.json'
    with manifest.open('xb') as stream: stream.write(json.dumps(files).encode('utf-8'))
    helper = Path(__file__).with_name('windows_launcher_admin.ps1')
    return dict(manifest=str(manifest), files=len(files),
        manifest_hash=hashlib.sha256(manifest.read_bytes()).hexdigest(), helper=str(helper),
        helper_hash=hashlib.sha256(helper.read_bytes()).hexdigest())


def ordinary(instance: str, root: Path) -> dict:
    WindowsSecurity().require_unprivileged()
    try:
        with (root / 'validation' / 'ordinary-write-denied').open('xb'):
            pass
    except PermissionError:
        pass
    else:
        raise AssertionError('ordinary user can modify the protected runtime')
    connection = HardwareConnection()
    try:
        state = connection.request('get_state', {})['state']
        if state['instance'] != instance or state['profile'] != 'windows-session-validation':
            raise RuntimeError('endpoint is not this isolated lifecycle fixture')
        assert state['supported'] is False and state['applied'] is None
        for method, params, code in (
            ('set_mode', {'mode': 'tent'}, 'unsupported-model'),
            ('session_update', dict(generation=state['generation'], ready=True,
                error_code=None, error_detail=None, unattended=False), 'access-denied')):
            try:
                connection.request(method, params)
            except HardwareOperationError as error:
                assert error.code == code
                if code == 'access-denied': break
            else:
                raise AssertionError('ordinary UI was allowed to control the inert fixture')
        return dict(success=True, same_fixture=True, no_modes=True,
            unregistered_receipt_denied=True, protected_tree_write_denied=True)
    finally:
        connection.close()


def system(root: Path) -> dict:
    security = WindowsSecurity()
    security.require_system()
    runtime = TrustedRuntime.current()
    if runtime.root != root.resolve(): raise ValueError('fixture root does not match pinned installation')
    result_root = root / 'validation'
    result_root.mkdir(exist_ok=True)
    engine = Engine(controller=Controller(),
        sensor=SimpleNamespace(info=SensorInfo(backend='windows-winrt'), reading=lambda: SensorReading()),
        actuator=SimpleNamespace(release=lambda: None), presentation=PosePresentation(), settings=None,
        device=DeviceInfo(id='windows-session-validation', platform='windows-validation', support_tier='unsupported'),
        hardware_query=lambda: (None, HardwareStatus.ABSENT))
    server = None
    supervisor = None
    launcher = None
    child = None
    registrations = []
    try:
        server = HardwareServer(engine)
        def register(peer):
            if peer is not None:
                assert supervisor.child is not None and not supervisor.child.resumed
                registrations.append(asdict(peer))
            server.register_session_process(peer)
        launcher = NativeSessionLauncher(runtime)
        token, console = launcher.api.console_token()
        try:
            if token is None or console is None or not console.unlocked:
                raise RuntimeError('unlocked local console required for this explicit test')
            if launcher.api.journal(token).exists():
                raise RuntimeError('existing user display journal; finish its recovery before testing')
        finally:
            if token is not None: launcher.api.close(token)
        supervisor = SessionSupervisor(launcher, register)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            engine.tick()
            server.poll()
            supervisor.poll()
            child = supervisor.child
            if (child is not None and child.resumed and engine.state.session_connected and
                    engine.session_owner == child.identity.owner):
                break
            time.sleep(.01)
        else:
            raise RuntimeError('trusted child did not obtain its lease: ' + str(supervisor.error))
        assert not launcher.api.process_elevated(child.process)
        assert child.identity == security.process_identity(child.identity.process_id)
        (result_root / 'ready.json').write_text(json.dumps(dict(instance=engine.state.instance,
            registered_before_resume=True, ordinary_child=True, lease_connected=True)), encoding='utf-8')
        # Leave a bounded window for the separate normal-user rejection checks.
        hold = time.monotonic() + 18
        while time.monotonic() < hold:
            engine.tick()
            server.poll()
            supervisor.poll()
            assert supervisor.child is child and child.alive()
            assert engine.state.applied is None and not engine.state.supported
            time.sleep(.01)
        supervisor.request_stop()
        assert engine.session_owner is None
        finish = time.monotonic() + 12
        while not supervisor.closed and time.monotonic() < finish:
            engine.tick()
            server.poll()
            supervisor.poll()
            time.sleep(.01)
        assert supervisor.closed and child.closed and launcher.pending_child is None
        assert registrations
        return dict(success=True, system=True, protected_runtime=True, ordinary_child=True,
            registered_before_resume=True, exact_child_identity=True, lease_connected=True,
            revoked_before_stop=True, child_reaped=True, hardware_supported=False,
            input_or_sensor_opened=False, driver_installed=False)
    finally:
        if supervisor is not None and not supervisor.closed:
            supervisor.request_stop()
            until = time.monotonic() + 12
            while not supervisor.closed and time.monotonic() < until:
                if server is not None: server.poll()
                supervisor.poll()
                time.sleep(.01)
        if launcher is not None: launcher.close()
        if server is not None: server.close()
        engine.shutdown()
        runtime.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--system', action='store_true')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--ordinary-instance')
    parser.add_argument('--prepare-runtime', type=Path)
    options = parser.parse_args()
    if sys.platform != 'win32': parser.error('requires authorized Windows tests')
    if sum((options.system, bool(options.ordinary_instance), options.prepare_runtime is not None)) != 1:
        parser.error('specify exactly one explicit fixture role')
    if options.prepare_runtime is not None:
        print(json.dumps(prepare(options.prepare_runtime)))
        return 0
    if options.ordinary_instance:
        if options.root is None: parser.error('ordinary fixture requires the protected root')
        print(json.dumps(ordinary(options.ordinary_instance, options.root)))
        return 0
    if not options.system or options.root is None: parser.error('specify one explicit fixture role')
    try:
        result = system(options.root)
    except BaseException as error:
        result = dict(success=False, error=str(error), traceback=traceback.format_exc())
    (options.root / 'validation/result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return 0 if result['success'] else 1


if __name__ == '__main__': raise SystemExit(main())
