"""Installed Start menu shortcuts keep the notification shell identity."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REPAIR_SCRIPT = ROOT / 'packaging/windows/repair-start-menu-shortcut.ps1'
APP_IDENTITY_SOURCE = ROOT / 'src/panasonic_posture/windows/app_identity.py'
MSI_BUILDER_SOURCE = ROOT / 'packaging/windows/build_msi.py'
APP_USER_MODEL_ID = 'work.mmoe.panasonic.PostureManager'


class ShortcutIdentityContractTests(unittest.TestCase):
    def test_installer_and_process_share_the_notification_identity(self):
        self.assertIn(
            f'APP_USER_MODEL_ID = "{APP_USER_MODEL_ID}"',
            APP_IDENTITY_SOURCE.read_text(encoding='utf-8'),
        )
        self.assertIn(
            f'Value="{APP_USER_MODEL_ID}"',
            MSI_BUILDER_SOURCE.read_text(encoding='utf-8'),
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
        self.install = self.root / 'install'
        self.service = self.root / 'service'
        self.runtime = self.service / 'runtime'
        for relative in (
            'launch.py',
            'data/assets/work.mmoe.panasonic.PostureManager.ico',
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

    def test_repair_writes_a_launchable_localized_shortcut(self):
        result = self.run_repair()
        self.assertEqual(result.returncode, 0, result.stderr)
        shortcut = (
            self.program_data
            / 'Microsoft/Windows/Start Menu/Programs'
            / '松下二合一平板电脑姿态管理工具'
            / '松下二合一平板电脑姿态管理工具.lnk'
        )
        self.assertTrue(shortcut.is_file())

        inspect = subprocess.run(
            [
                shutil.which('powershell.exe'),
                '-NoLogo',
                '-NoProfile',
                '-NonInteractive',
                '-Command',
                (
                    '$s=(New-Object -ComObject WScript.Shell).CreateShortcut('
                    f"'{shortcut}'); "
                    'Write-Output $s.TargetPath; Write-Output $s.Arguments'
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
        )
        self.assertEqual(inspect.returncode, 0, inspect.stderr)
        target, arguments = inspect.stdout.splitlines()
        self.assertTrue(
            os.path.samefile(target, self.runtime / 'pythonw.exe')
        )
        self.assertTrue(arguments.startswith('-I "'))
        self.assertTrue(arguments.endswith('\\launch.py"'))

    def test_invalid_language_is_rejected_before_creating_a_shortcut(self):
        result = self.run_repair(0x10000)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('LanguageId', result.stderr)
        programs = (
            self.program_data
            / 'Microsoft/Windows/Start Menu/Programs'
        )
        self.assertFalse(programs.exists())


if __name__ == '__main__':
    unittest.main()
