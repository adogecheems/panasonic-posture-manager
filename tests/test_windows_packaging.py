"""Portable Windows bundle assembly, using a synthetic runtime on any host."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

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
            archive = builder.build(root / 'output', runtime, dependencies)
            with zipfile.ZipFile(archive) as bundle:
                prefix = 'PanasonicPostureManager/'
                names = set(bundle.namelist())
                for name in ('runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python._pth',
                             'runtime/pythonw._pth', 'runtime/python311._pth',
                             'app/panasonic_posture/windows/client.py', 'data/assets/lets-note-window-icon.png',
                             'po/zh_CN.po', 'LICENSE', 'runtime/Lib/site-packages/Qt-1.dist-info/licenses/LICENSE',
                             'Panasonic Posture Manager.cmd', 'Simulation.cmd'):
                    self.assertIn(prefix + name, names)
                self.assertFalse(any('host_secret' in name or 'pip.exe' in name for name in names))
                self.assertIn(b'../app', bundle.read(prefix + 'runtime/python._pth'))
                manifest = json.loads(bundle.read(prefix + 'MANIFEST.json'))
                self.assertEqual(set(manifest), {name[len(prefix):] for name in names} - {'MANIFEST.json'})
                for name, digest in manifest.items():
                    self.assertEqual(hashlib.sha256(bundle.read(prefix + name)).hexdigest(), digest)
            self.assertEqual(archive.with_suffix('.zip.sha256').read_text().split()[0],
                             hashlib.sha256(archive.read_bytes()).hexdigest())

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
