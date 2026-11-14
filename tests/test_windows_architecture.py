"""Import-boundary checks for the refactored Windows layers."""
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src'


def run_isolated(source: str) -> subprocess.CompletedProcess:
    bootstrap = f'import sys; sys.path.insert(0, {str(SOURCE)!r})\n'
    return subprocess.run(
        [sys.executable, '-S', '-c', bootstrap + textwrap.dedent(source)],
        capture_output=True,
        text=True,
        timeout=10,
    )


BLOCKER = '''
import sys
class Block:
    def find_spec(self, name, path=None, target=None):
        if name in blocked:
            raise AssertionError(name)
sys.meta_path.insert(0, Block())
'''


class WindowsArchitectureTests(unittest.TestCase):
    def test_windows_composition_has_no_machine_identity_allowlist(self):
        root = SOURCE / 'panasonic_posture'
        for relative in (
            'windows/service/application.py',
            'windows/hardware/machine_profile.py',
            'windows/session/screen_session.py',
            'windows/ui/app.py',
        ):
            with self.subTest(relative=relative):
                source = (root / relative).read_text(encoding='utf-8')
                self.assertNotIn('SUPPORTED_WINDOWS_MACHINE', source)
                self.assertNotIn('verified_machine', source)

    def test_legacy_development_backend_is_absent_from_the_product(self):
        windows = SOURCE / 'panasonic_posture/windows'
        for name in ('backend.py', 'private_backend.py', 'simulation.py'):
            with self.subTest(name=name):
                self.assertFalse(list(windows.rglob(name)))
        self.assertFalse((windows / 'client.py').exists())
        self.assertFalse(list(windows.rglob('demo.py')))

    def test_hardware_composition_does_not_load_ui_or_screen_session_core(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.ipc.client',
    'panasonic_posture.windows.service.server',
    'panasonic_posture.windows.session.screen_session',
    'panasonic_posture.windows.common.security',
    'panasonic_posture.windows.service.launch',
    'panasonic_posture.windows.service.supervisor',
    'panasonic_posture.windows.service.trusted_runtime',
    'panasonic_posture.windows.ui.app',
}
from panasonic_posture.windows.service.runtime import HardwareRuntime
assert HardwareRuntime
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ui_rpc_and_protocol_do_not_load_the_privileged_server(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.service.host',
    'panasonic_posture.windows.service.runtime',
    'panasonic_posture.windows.service.server',
    'panasonic_posture.windows.session.agent',
    'panasonic_posture.windows.service.launch',
}
from panasonic_posture.windows.ipc.client import HardwareUIClient
from panasonic_posture.windows.ipc.protocol import validate_request
from panasonic_posture.windows.ipc.rpc import HardwareConnection
assert HardwareUIClient and HardwareConnection and validate_request
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hardware_protocol_does_not_depend_on_the_frontend(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.frontend',
    'panasonic_posture.frontend.ports',
}
from panasonic_posture.windows.ipc.protocol import validate_request
assert validate_request
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_machine_profile_has_no_session_or_privilege_dependency(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.service.runtime',
    'panasonic_posture.windows.common.native_preflight',
    'panasonic_posture.windows.session.screen_session',
    'panasonic_posture.windows.common.security',
    'panasonic_posture.windows.session.agent',
}
from panasonic_posture.windows.hardware.machine_profile import resolve_windows_profile
assert resolve_windows_profile
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_session_supervisor_depends_on_ports_not_native_launching(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.ipc.pipe',
    'panasonic_posture.windows.service.server',
    'panasonic_posture.windows.common.security',
    'panasonic_posture.windows.service.launch',
    'panasonic_posture.windows.common.stop_event',
    'panasonic_posture.windows.service.trusted_runtime',
}
from panasonic_posture.windows.service.supervisor import SessionSupervisor
assert SessionSupervisor
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_startup_launcher_does_not_load_fluent_pages_or_dependencies(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.ui.app',
    'qfluentwidgets',
    'qframelesswindow',
}
from panasonic_posture.windows.ui.launcher import run
assert run
''')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
