"""Installed Start menu shortcuts keep the notification shell identity."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
REPAIR_SCRIPT = ROOT / 'packaging/windows/msi/repair-start-menu-shortcut.ps1'
APP_IDENTITY_SOURCE = ROOT / 'src/panasonic_posture/windows/app/identity.py'
APP_USER_MODEL_ID = 'work.mmoe.panasonic.PostureManager'
MSI_BUILDER_SPEC = importlib.util.spec_from_file_location(
    'windows_shortcut_msi_build',
    ROOT / 'packaging/windows/scripts/build_msi.py',
)
MSI_BUILDER = importlib.util.module_from_spec(MSI_BUILDER_SPEC)
MSI_BUILDER_SPEC.loader.exec_module(MSI_BUILDER)


class ShortcutIdentityContractTests(unittest.TestCase):
    def test_installer_and_process_share_the_notification_identity(self):
        self.assertIn(
            f'APP_USER_MODEL_ID = "{APP_USER_MODEL_ID}"',
            APP_IDENTITY_SOURCE.read_text(encoding='utf-8'),
        )
        generated = ET.fromstring(MSI_BUILDER.render_components({
            'main': {},
            'service': {'runtime/pythonw.exe': Path('fixture')},
        }))
        properties = {
            element.get('Key'): element.get('Value')
            for element in generated.iter()
            if element.tag.endswith('ShortcutProperty')
        }
        self.assertEqual(
            properties.get('System.AppUserModel.ID'),
            APP_USER_MODEL_ID,
        )
        repair = REPAIR_SCRIPT.read_text(encoding='utf-8')
        self.assertIn(f"$appUserModelId = '{APP_USER_MODEL_ID}'", repair)
        self.assertIn('System.AppUserModel.ID', repair)


@unittest.skipUnless(sys.platform == 'win32', 'shortcut property storage is Windows-only')
class ShortcutRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.program_data = self.root / 'ProgramData'
        self.programs = (
            self.program_data / 'Microsoft/Windows/Start Menu/Programs'
        )
        self.programs.mkdir(parents=True)
        self.install = self.root / 'install'
        self.service = self.root / 'service'
        self.runtime = self.service / 'runtime'
        for relative in (
            'launch.py',
            'data/assets/icons/work.mmoe.panasonic.PostureManager.ico',
        ):
            path = self.install / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'fixture')
        (self.runtime / 'pythonw.exe').parent.mkdir(parents=True)
        (self.runtime / 'pythonw.exe').write_bytes(b'fixture')
        self.installed_repair = self.service / REPAIR_SCRIPT.name
        shutil.copy2(REPAIR_SCRIPT, self.installed_repair)

    def run_repair(self, language_id: int = 0x0804):
        powershell = shutil.which('powershell.exe')
        if powershell is None:
            self.skipTest('Windows PowerShell is unavailable')
        environment = os.environ.copy()
        environment['ProgramData'] = str(self.program_data)
        return subprocess.run(
            [
                powershell,
                '-NoLogo',
                '-NoProfile',
                '-NonInteractive',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                str(self.installed_repair),
                '-LanguageId',
                str(language_id),
                '-InstallFolder',
                str(self.install),
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
        )

    def inspect_shortcut(self, shortcut: Path) -> tuple[str, str, str]:
        powershell = shutil.which('powershell.exe')
        if powershell is None:
            self.skipTest('Windows PowerShell is unavailable')
        output = self.root / 'shortcut-inspection.txt'
        # WScript's legacy path conversion must not limit the independent reader.
        inspection_link = self.root / 'inspection.lnk'
        shutil.copy2(shortcut, inspection_link)
        environment = os.environ.copy()
        environment['SHORTCUT_PATH'] = str(inspection_link)
        environment['SHORTCUT_INSPECTION'] = str(output)
        result = subprocess.run(
            [
                powershell,
                '-NoLogo',
                '-NoProfile',
                '-NonInteractive',
                '-Command',
                (
                    '$s=(New-Object -ComObject WScript.Shell).'
                    'CreateShortcut($env:SHORTCUT_PATH); '
                    '[IO.File]::WriteAllLines('
                    '$env:SHORTCUT_INSPECTION, '
                    '@($s.TargetPath, $s.Arguments, $s.Description), '
                    '[Text.UTF8Encoding]::new($false))'
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        target, arguments, _legacy_description = output.read_text(
            encoding='utf-8'
        ).splitlines()
        # WScript also converts descriptions through the system code page.
        # Read NAME_STRING independently from the saved MS-SHLLINK StringData:
        # https://learn.microsoft.com/openspecs/windows_protocols/ms-shllink/17b69472-0f34-4bcf-b290-eccdb8de224b
        data = shortcut.read_bytes()
        self.assertEqual(struct.unpack_from('<I', data)[0], 0x4C)
        flags = struct.unpack_from('<I', data, 0x14)[0]
        self.assertTrue(flags & 0x04, 'shortcut must contain a description')
        self.assertTrue(flags & 0x80, 'shortcut must store Unicode strings')
        offset = 0x4C
        if flags & 0x01:  # LinkTargetIDList precedes LinkInfo and StringData.
            offset += 2 + struct.unpack_from('<H', data, offset)[0]
        if flags & 0x02:
            offset += struct.unpack_from('<I', data, offset)[0]
        length = struct.unpack_from('<H', data, offset)[0]
        description = data[offset + 2:offset + 2 + length * 2].decode('utf-16-le')
        return target, arguments, description

    def test_repair_writes_a_launchable_localized_shortcut(self):
        result = self.run_repair()
        self.assertEqual(result.returncode, 0, result.stderr)
        shortcut = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
        self.assertTrue(shortcut.is_file())

        target, arguments, description = self.inspect_shortcut(shortcut)
        self.assertTrue(
            os.path.samefile(target, self.runtime / 'pythonw.exe')
        )
        self.assertTrue(arguments.startswith('-B -I "'))
        self.assertTrue(arguments.endswith('\\launch.py"'))
        self.assertEqual(
            description,
            '打开 松下二合一平板电脑姿态管理工具',
        )

    def test_traditional_chinese_uses_english_shortcut_copy(self):
        result = self.run_repair(0x0404)
        self.assertEqual(result.returncode, 0, result.stderr)
        shortcut = (
            self.programs
            / 'Panasonic 2-in-1 Tablet Posture Management Tool.lnk'
        )
        self.assertTrue(shortcut.is_file())
        self.assertEqual(
            self.inspect_shortcut(shortcut)[2],
            'Open Panasonic 2-in-1 Tablet Posture Management Tool',
        )

    def test_missing_programs_directory_is_created(self):
        self.programs.rmdir()
        result = self.run_repair()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(
            (self.programs / '松下二合一平板电脑姿态管理工具.lnk').is_file()
        )

    def test_unicode_path_outside_system_code_page_and_repair_are_supported(self):
        self.program_data = self.root / 'Программы-😀'
        self.programs = self.program_data / 'Microsoft/Windows/Start Menu/Programs'
        self.programs.mkdir(parents=True)
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                result = self.run_repair()
                self.assertEqual(result.returncode, 0, result.stderr)
                shortcut = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
                self.assertTrue(shortcut.is_file())
                target, arguments, description = self.inspect_shortcut(shortcut)
                self.assertTrue(os.path.samefile(target, self.runtime / 'pythonw.exe'))
                self.assertTrue(arguments.startswith('-B -I "'))
                self.assertTrue(arguments.endswith('\\launch.py"'))
                self.assertTrue(
                    os.path.samefile(arguments[7:-1], self.install / 'launch.py')
                )
                self.assertEqual(
                    description, '打开 松下二合一平板电脑姿态管理工具'
                )

    def test_invalid_language_is_rejected_before_creating_a_shortcut(self):
        result = self.run_repair(0x10000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('LanguageId', result.stderr)
        self.assertEqual(list(self.programs.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
