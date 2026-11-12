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
    def test_legacy_development_backend_is_absent_from_the_product(self):
        windows = SOURCE / 'panasonic_posture/windows'
        for name in ('backend.py', 'client.py', 'private_backend.py', 'simulation.py'):
            with self.subTest(name=name):
                self.assertFalse((windows / name).exists())
        self.assertFalse((SOURCE / 'panasonic_posture/fluent/demo.py').exists())

    def test_hardware_composition_does_not_load_ui_or_screen_session_core(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.hardware_client',
    'panasonic_posture.windows.hardware_server',
    'panasonic_posture.windows.screen_session',
    'panasonic_posture.windows.security',
    'panasonic_posture.windows.session_launch',
    'panasonic_posture.windows.session_supervisor',
    'panasonic_posture.windows.trusted_runtime',
    'panasonic_posture.fluent.app',
}
from panasonic_posture.windows.hardware_runtime import HardwareRuntime
assert HardwareRuntime
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ui_rpc_and_protocol_do_not_load_the_privileged_server(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.hardware_host',
    'panasonic_posture.windows.hardware_runtime',
    'panasonic_posture.windows.hardware_server',
    'panasonic_posture.windows.session_agent',
    'panasonic_posture.windows.session_launch',
}
from panasonic_posture.windows.hardware_client import HardwareUIClient
from panasonic_posture.windows.hardware_protocol import validate_request
from panasonic_posture.windows.hardware_rpc import HardwareConnection
assert HardwareUIClient and HardwareConnection and validate_request
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_hardware_protocol_does_not_depend_on_the_frontend(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.frontend',
    'panasonic_posture.frontend.ports',
}
from panasonic_posture.windows.hardware_protocol import validate_request
assert validate_request
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_machine_profile_has_no_session_or_privilege_dependency(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.hardware_runtime',
    'panasonic_posture.windows.native_preflight',
    'panasonic_posture.windows.screen_session',
    'panasonic_posture.windows.security',
    'panasonic_posture.windows.session_agent',
}
from panasonic_posture.windows.machine_profile import ProfileSensor, verified_machine
assert ProfileSensor and verified_machine
''')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_session_supervisor_depends_on_ports_not_native_launching(self):
        result = run_isolated(BLOCKER + '''
blocked = {
    'panasonic_posture.windows.hardware_pipe',
    'panasonic_posture.windows.hardware_server',
    'panasonic_posture.windows.security',
    'panasonic_posture.windows.session_launch',
    'panasonic_posture.windows.session_stop',
    'panasonic_posture.windows.trusted_runtime',
}
from panasonic_posture.windows.session_supervisor import SessionSupervisor
assert SessionSupervisor
''')
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
