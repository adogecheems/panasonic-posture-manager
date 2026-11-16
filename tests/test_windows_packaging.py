"""Windows application payload assembly, independent of portable ZIP policy."""
import codecs
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_build', ROOT / 'packaging/windows/build.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class WindowsPackagingTests(unittest.TestCase):
    def test_packaged_powershell_with_non_ascii_is_utf8_bom(self):
        for script in sorted((ROOT / 'packaging/windows').rglob('*.ps1')):
            with self.subTest(script=script.relative_to(ROOT).as_posix()):
                source = script.read_bytes()
                if not source.isascii():
                    self.assertTrue(
                        source.startswith(codecs.BOM_UTF8),
                        'Windows PowerShell 5.1 must recognize non-ASCII '
                        'script source as UTF-8',
                    )

    def test_ui_requirements_pin_the_supported_qt_lts_runtime(self):
        requirements = (ROOT / 'packaging/windows/requirements-ui.txt').read_text(
            encoding='utf-8'
        )
        for requirement in (
            'PyQt6==6.8.1',
            'PyQt6-Qt6==6.8.2',
            'PyQt6-Fluent-Widgets==1.11.3',
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, requirements)

    def test_payload_contains_resources_licenses_and_isolated_runtimes(self):
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
            for relative in builder.PYWIN32_REQUIRED_FILES:
                path = dependencies / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'pywin32')
            for name in ('pywintypes312.dll', 'pythoncom312.dll'):
                path = dependencies / 'pywin32_system32' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'pywin32')
            (dependencies / 'Qt-1.dist-info/licenses/LICENSE').write_text('license', encoding='utf-8')
            pyqt_metadata = dependencies / 'PyQt6-6.8.1.dist-info'
            pyqt_metadata.mkdir()
            (pyqt_metadata / 'METADATA').write_text(
                'Name: PyQt6\nVersion: 6.8.1\nLicense: GPL v3\n',
                encoding='utf-8',
            )
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
            files = builder.application_files(runtime, dependencies)
            for name in ('postures/laptop.svg', 'branding/lets-note-window-icon.png',
                         'icons/work.mmoe.panasonic.PostureManager.png'):
                self.assertEqual(files['data/assets/' + name], ROOT / 'data/assets/common' / name)
            for name in ('runtime/python.exe', 'runtime/pythonw.exe', 'runtime/python._pth',
                         'runtime/pythonw._pth', 'runtime/python-hardware.exe',
                         'runtime/python-hardware._pth',
                         'app/panasonic_posture/windows/ipc/client.py',
                         'app/panasonic_posture/windows/app/update_check.py',
                         'app/panasonic_posture/windows/ui/update_check.py',
                         'data/assets/branding/lets-note-window-icon.png',
                         'data/assets/icons/work.mmoe.panasonic.PostureManager.ico',
                         'data/assets/icons/work.mmoe.panasonic.PostureManager-square.png',
                         'data/assets/postures/laptop.svg',
                         'data/assets/postures/tablet.svg',
                         'data/assets/postures/tent.svg',
                         'data/assets/postures/stand.svg',
                         'data/profiles/cfqv8.json',
                         'data/profiles/cfqv9.json',
                         'data/profiles/cfqv1.json',
                         'po/zh_CN.po', 'LICENSE',
                         'runtime/Lib/site-packages/Qt-1.dist-info/licenses/LICENSE',
                         'runtime/Lib/site-packages/PyQt6-6.8.1.dist-info/LICENSE',
                         'runtime/Lib/site-packages/win32/win32api.pyd',
                         'runtime/Lib/site-packages/win32comext/shell/shell.pyd',
                         'runtime/Lib/site-packages/pywin32_system32/pywintypes312.dll',
                         f'{builder.APPLICATION_DISPLAY_NAME}.cmd', 'Hardware.cmd'):
                self.assertIn(name, files)
            self.assertNotIn('data/assets/notification-badge.svg', files)
            self.assertFalse(any(name.startswith('data/assets/installer/') for name in files))
            self.assertEqual(
                files['runtime/Lib/site-packages/PyQt6-6.8.1.dist-info/LICENSE'],
                ROOT / 'packaging/windows/PyQt6-LICENSE',
            )
            self.assertFalse(any('host_secret' in name or 'pip.exe' in name for name in files))
            self.assertFalse(any('/qml/' in name for name in files))
            self.assertFalse(any('/test/' in name or '/tests/' in name or
                                 '/demo/' in name or '/demos/' in name for name in files))
            self.assertFalse(any('/turtledemo/' in name or '/idle_test/' in name
                                 for name in files))
            self.assertFalse(any(name.endswith('.ps1') for name in files))
            self.assertFalse(any('Hardware Service' in name for name in files))
            self.assertFalse(any('/app/panasonic_posture/linux/' in name for name in files))
            for retired in (
                'backend.py',
                'private_backend.py',
                'simulation.py',
            ):
                with self.subTest(retired=retired):
                    self.assertFalse(
                        any(
                            name.startswith('app/panasonic_posture/windows/')
                            and name.endswith('/' + retired)
                            for name in files
                        )
                    )
            self.assertFalse(
                any(name.endswith('/windows/ui/demo.py') for name in files)
            )
            self.assertNotIn('app/panasonic_posture/windows/client.py', files)
            self.assertNotIn('runtime/python311._pth', files)
            self.assertLess(max(map(len, files)), 240)
            native = files['Hardware.cmd']
            self.assertIsInstance(native, bytes)
            native_text = native.decode('utf-8')
            self.assertIn('runtime\\pythonw.exe', native_text)
            self.assertIn('-B -I', native_text)
            self.assertNotIn('--hardware-backend', native_text)
            project_bytecode = (
                'app/panasonic_posture/windows/ui/__pycache__/'
                f'launcher.{sys.implementation.cache_tag}.pyc'
            )
            ui_bytecode = (
                'runtime/Lib/site-packages/qfluentwidgets/'
                f'__pycache__/__init__.{sys.implementation.cache_tag}.pyc'
            )
            self.assertIn(project_bytecode, files)
            self.assertIn(ui_bytecode, files)
            compiled = files[project_bytecode]
            self.assertIsInstance(compiled, bytes)
            self.assertEqual(
                int.from_bytes(compiled[4:8], 'little'),
                1,  # Unchecked hash flag: never consult source mtime at runtime.
            )
            launcher = files['launch.py']
            self.assertIsInstance(launcher, bytes)
            launcher_text = launcher.decode('utf-8')
            self.assertIn(
                'Path(__file__).resolve().parent / "app"',
                launcher_text,
            )
            self.assertLess(
                launcher_text.index('sys.path.insert'),
                launcher_text.index('from panasonic_posture'),
            )
            self.assertIn(b'../app', files['runtime/python._pth'])
            self.assertIn(b'Lib/site-packages', files['runtime/python._pth'])
            self.assertEqual(files['runtime/python-hardware._pth'],
                             b'Lib\nDLLs\n../app\n')

    def test_missing_windows_frameless_window_transitives_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            dependencies = Path(directory)
            missing = builder._missing_pywin32_files(dependencies)
            self.assertIn('win32/win32api.pyd', missing)
            self.assertIn('pywin32_system32/pywintypes*.dll', missing)
            for relative in builder.PYWIN32_REQUIRED_FILES:
                path = dependencies / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'pywin32')
            for name in ('pywintypes312.dll', 'pythoncom312.dll'):
                path = dependencies / 'pywin32_system32' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'pywin32')
            self.assertEqual(builder._missing_pywin32_files(dependencies), [])

    def test_missing_runtime_and_missing_licenses_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, 'runtime is missing'):
                builder.application_files(root / 'absent', root / 'deps')
            runtime, dependencies = root / 'python', root / 'dependencies'
            for name in ('python.exe', 'pythonw.exe', 'LICENSE.txt', 'Lib/os.py'):
                path = runtime / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic runtime', encoding='utf-8')
            for name in ('PyQt6', 'qfluentwidgets', 'qframelesswindow', 'Qt-1.dist-info'):
                (dependencies / name).mkdir(parents=True)
            for relative in builder.PYWIN32_REQUIRED_FILES:
                path = dependencies / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'pywin32')
            for name in ('pywintypes312.dll', 'pythoncom312.dll'):
                path = dependencies / 'pywin32_system32' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'pywin32')
            with self.assertRaisesRegex(ValueError, 'license metadata is missing'):
                builder.application_files(runtime, dependencies)


if __name__ == '__main__':
    unittest.main()
