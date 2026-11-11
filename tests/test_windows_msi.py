"""Windows MSI payload, service registration and release-source contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    'windows_msi_build', ROOT / 'packaging/windows/build_msi.py'
)
msi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(msi)


class MsiPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / 'python'
        self.dependencies = self.root / 'dependencies'
        for name in ('python.exe', 'pythonw.exe', 'LICENSE.txt', 'Lib/os.py',
                     'Lib/a&b.py'):
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('runtime', encoding='utf-8')
        for name in ('PyQt6', 'qfluentwidgets', 'qframelesswindow',
                     'Qt-1.dist-info/licenses'):
            (self.dependencies / name).mkdir(parents=True)
        (self.dependencies / 'Qt-1.dist-info/licenses/LICENSE').write_text(
            'license', encoding='utf-8'
        )
        (self.dependencies / 'qfluentwidgets/__init__.py').write_text(
            '', encoding='utf-8'
        )

    def test_payload_contains_runtime_and_excludes_manual_service_helpers(self):
        payload = self.root / 'payload'
        manifest = msi.stage_payload(payload, self.runtime, self.dependencies)
        self.assertIn('runtime/python-hardware.exe', manifest)
        self.assertIn('runtime/python-hardware._pth', manifest)
        self.assertIn('app/panasonic_posture/windows/hardware_service.py', manifest)
        self.assertIn('configure-slate-device.ps1', manifest)
        self.assertIn('hardware-service.ps1', manifest)
        self.assertNotIn('Install Hardware Service.cmd', manifest)
        self.assertNotIn('Configure Slate Device.cmd', manifest)
        self.assertEqual(
            json.loads((payload / 'MANIFEST.json').read_text(encoding='utf-8')),
            manifest,
        )
        for name, digest in manifest.items():
            self.assertEqual(
                hashlib.sha256((payload / name).read_bytes()).hexdigest(),
                digest,
            )

    def test_generated_components_are_deterministic_and_register_the_service(self):
        files = msi.payload_files(self.runtime, self.dependencies)
        first = msi.render_components(files)
        second = msi.render_components(files)
        self.assertEqual(first, second)
        root = ET.fromstring(first)
        self.assertTrue(root.tag.endswith('Wix'))
        self.assertIn('Name="a&amp;b.py"', first)
        self.assertIn('Id="PythonHardwareExe"', first)
        self.assertIn('Id="HardwareServiceScript"', first)
        self.assertIn('Id="ApplicationStartMenuShortcut"', first)
        self.assertIn('Advertise="yes"', first)
        self.assertIn('Name="PanasonicPostureManagerHardware"', first)
        self.assertIn('Account="LocalSystem"', first)
        self.assertIn('panasonic_posture.windows.hardware_service --serve', first)
        self.assertIn('Start="install"', first)
        self.assertIn('Remove="uninstall"', first)

    def test_package_template_installs_full_runtime_and_reversible_csm_setup(self):
        source = (ROOT / 'packaging/windows/msi/Package.wxs').read_text(encoding='utf-8')
        root = ET.fromstring(source)
        self.assertTrue(root.tag.endswith('Wix'))
        for value in (
            'Scope="perMachine"',
            'Id="ConfigureSlate"',
            '-Action Install',
            'Id="ConfigureHardwareService"',
            '-Action Configure',
            '[System64Folder]WindowsPowerShell',
            'Id="RestoreSlate"',
            '-Action Remove',
            'Id="RollbackSlate"',
            'Id="SearchConfiguredSlate"',
            'SlateExclusiveConfigured',
            'Id="SearchWindowsBuild"',
            'WINDOWS_BUILD &gt;= 10240',
            'Id="ProgramMenuFolder"',
            'ScheduleReboot',
            'Id="WixUI_Minimal"',
        ):
            with self.subTest(value=value):
                self.assertIn(value, source)

    @unittest.skipUnless(sys.platform == 'win32', 'MSI build runs on Windows')
    def test_failed_msi_build_preserves_the_existing_artifact(self):
        output = self.root / 'output'
        output.mkdir()
        original = output / 'panasonic-posture-manager-2.4.0-windows-x64.msi'
        original.write_bytes(b'existing')

        def fail(command, **options):
            self.assertIn('WixToolset.UI.wixext', command)
            return None

        with self.assertRaisesRegex(OSError, 'WiX did not create'):
            msi.build(
                output,
                self.runtime,
                self.dependencies,
                version='2.4.0',
                runner=fail,
            )
        self.assertEqual(original.read_bytes(), b'existing')


if __name__ == '__main__':
    unittest.main()
