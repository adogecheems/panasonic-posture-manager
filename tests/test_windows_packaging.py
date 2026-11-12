"""Portable Windows bundle assembly, using a synthetic runtime on any host."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_build', ROOT / 'packaging/windows/build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class WindowsPackagingTests(unittest.TestCase):
    def test_bundle_contains_resources_licenses_isolated_runtime_and_checksums(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, dependencies = root / 'python', root / 'dependencies'
            for name in ('python.exe', 'pythonw.exe', 'python311.dll', 'LICENSE.txt', 'Lib/os.py',
                         'Lib/site-packages/host_secret.py', 'Scripts/pip.exe'):
                path = runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic runtime', encoding='utf-8')
            for name in ('PyQt6', 'qfluentwidgets', 'qframelesswindow', 'Qt-1.dist-info/licenses'):
                (dependencies / name).mkdir(parents=True)
            (dependencies / 'Qt-1.dist-info/licenses/LICENSE').write_text('license', encoding='utf-8')
            (dependencies / 'qfluentwidgets/__init__.py').write_text('', encoding='utf-8')
            qml = dependencies / 'PyQt6/Qt6/qml/Unused/Long/Path/asset.png'
            qml.parent.mkdir(parents=True)
            qml.write_bytes(b'not part of the widget runtime')
            for relative in ('win32com/test/test_private.py', 'win32com/demos/demo.py'):
                path = dependencies / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(b'not part of the application')
            for relative in ('Lib/turtledemo/turtle.py', 'Lib/idlelib/idle_test/test_private.py'):
                path = runtime / relative
                path.parent.mkdir(parents=True)
                path.write_bytes(b'not part of the application')
            # Runtime DLLs and the complete archive may be large. Building
            # must hash and write bounded chunks instead of loading them whole.
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded build read')):
                archive = builder.build(root / 'output', runtime, dependencies)
            self.assertEqual(archive.name, 'panasonic-posture-windows-portable.zip')
            staged = root / 'staged'
            builder.stage_application(staged, runtime, dependencies)
            with zipfile.ZipFile(archive) as bundle:
                prefix = 'PanasonicPostureManager/'
                names = set(bundle.namelist())
                for name in ('runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python._pth',
                             'runtime/pythonw._pth', 'runtime/python-hardware.exe',
                             'runtime/python-hardware._pth',
                             'app/panasonic_posture/windows/hardware_client.py',
                             'app/panasonic_posture/windows/update_check.py',
                             'app/panasonic_posture/fluent/update_check.py',
                             'data/assets/lets-note-window-icon.png',
                             'data/assets/work.mmoe.panasonic.PostureManager.ico',
                             'data/assets/notification-badge.svg',
                             'po/zh_CN.po', 'LICENSE', 'runtime/Lib/site-packages/Qt-1.dist-info/licenses/LICENSE',
                             f'{builder.APPLICATION_DISPLAY_NAME}.cmd', 'Hardware.cmd'):
                    self.assertIn(prefix + name, names)
                self.assertFalse(any('host_secret' in name or 'pip.exe' in name for name in names))
                self.assertFalse(any('/qml/' in name for name in names))
                self.assertFalse(any('/test/' in name or '/tests/' in name or
                                     '/demo/' in name or '/demos/' in name for name in names))
                self.assertFalse(any('/turtledemo/' in name or '/idle_test/' in name
                                     for name in names))
                self.assertFalse(any(name.endswith('.ps1') for name in names))
                self.assertFalse(any('Hardware Service' in name for name in names))
                for retired in (
                    'app/panasonic_posture/fluent/demo.py',
                    'app/panasonic_posture/windows/backend.py',
                    'app/panasonic_posture/windows/client.py',
                    'app/panasonic_posture/windows/private_backend.py',
                    'app/panasonic_posture/windows/simulation.py',
                ):
                    with self.subTest(retired=retired):
                        self.assertNotIn(prefix + retired, names)
                self.assertNotIn(prefix + 'runtime/python311._pth', names)
                self.assertLess(max(map(len, names)), 240)
                native = bundle.read(prefix + 'Hardware.cmd').decode('utf-8')
                self.assertIn('runtime\\pythonw.exe', native)
                self.assertNotIn('--hardware-backend', native)
                launcher = bundle.read(prefix + 'launch.py').decode('utf-8')
                self.assertIn(
                    'Path(__file__).resolve().parent / "app"',
                    launcher,
                )
                self.assertLess(
                    launcher.index('sys.path.insert'),
                    launcher.index('from panasonic_posture'),
                )
                self.assertIn(b'../app', bundle.read(prefix + 'runtime/python._pth'))
                self.assertIn(b'Lib/site-packages', bundle.read(prefix + 'runtime/python._pth'))
                self.assertEqual(bundle.read(prefix + 'runtime/python-hardware._pth'),
                                 b'Lib\nDLLs\n../app\n')
                manifest = json.loads(bundle.read(prefix + 'MANIFEST.json'))
                self.assertEqual(set(manifest), {name[len(prefix):] for name in names} - {'MANIFEST.json'})
                for name, digest in manifest.items():
                    self.assertEqual(hashlib.sha256(bundle.read(prefix + name)).hexdigest(), digest)
                staged_files = {path.relative_to(staged).as_posix(): path.read_bytes()
                                for path in staged.rglob('*') if path.is_file()}
                self.assertEqual({name[len(prefix):]: bundle.read(name) for name in names}, staged_files)
            self.assertEqual(archive.with_suffix('.zip.sha256').read_text().split()[0],
                             hashlib.sha256(archive.read_bytes()).hexdigest())
            original = archive.read_bytes()
            checksum = archive.with_suffix('.zip.sha256').read_bytes()
            with patch.object(zipfile.ZipFile, 'open', side_effect=OSError('disk full')):
                with self.assertRaisesRegex(OSError, 'disk full'):
                    builder.build(root / 'output', runtime, dependencies)
            self.assertEqual(archive.read_bytes(), original)
            self.assertEqual(archive.with_suffix('.zip.sha256').read_bytes(), checksum)
            self.assertFalse(list((root / 'output').glob('posture-windows-*')))

    def test_missing_runtime_and_missing_licenses_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'runtime is missing'):
                builder.stage_application(root / 'bundle', root / 'absent', root / 'deps')
            self.assertFalse((root / 'bundle').exists())
            runtime, dependencies = root / 'python', root / 'dependencies'
            for name in ('python.exe', 'pythonw.exe', 'LICENSE.txt', 'Lib/os.py'):
                path = runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic runtime', encoding='utf-8')
            for name in ('PyQt6', 'qfluentwidgets', 'qframelesswindow', 'Qt-1.dist-info'):
                (dependencies / name).mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'license metadata is missing'):
                builder.stage_application(root / 'bundle', runtime, dependencies)
            self.assertFalse((root / 'bundle').exists())


if __name__ == '__main__':
    unittest.main()
