"""Windows MSI payload, service registration and release-source contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
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


def png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:8] != b'\x89PNG\r\n\x1a\n' or data[12:16] != b'IHDR':
        raise ValueError(f'{path} is not a PNG')
    return int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')


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
        main = manifest['main']
        service = manifest['service']
        self.assertIn('launch.py', main)
        self.assertIn('app/panasonic_posture/windows/hardware_client.py', main)
        self.assertNotIn('runtime/python-hardware.exe', main)
        self.assertIn('runtime/python-hardware.exe', service)
        self.assertIn('runtime/python-hardware._pth', service)
        self.assertIn('app/panasonic_posture/windows/hardware_service.py', service)
        self.assertIn('configure-slate-device.ps1', service)
        self.assertIn('hardware-service.ps1', service)
        self.assertNotIn('Install Hardware Service.cmd', service)
        self.assertNotIn('Configure Slate Device.cmd', service)
        for role, expected in manifest.items():
            self.assertEqual(
                json.loads(
                    (payload / role / 'MANIFEST.json').read_text(encoding='utf-8')
                ),
                expected,
            )
            for name, digest in expected.items():
                self.assertEqual(
                    hashlib.sha256((payload / role / name).read_bytes()).hexdigest(),
                    digest,
                )

    def test_generated_components_are_deterministic_and_register_the_service(self):
        groups = msi.payload_files(self.runtime, self.dependencies)
        first = msi.render_components(groups)
        second = msi.render_components(groups)
        self.assertEqual(first, second)
        root = ET.fromstring(first)
        self.assertTrue(root.tag.endswith('Wix'))
        parents = {
            child: parent
            for parent in root.iter()
            for child in parent
        }
        shortcut = next(
            element for element in root.iter()
            if element.tag.endswith('Shortcut')
            and element.get('Id') == 'ApplicationStartMenuShortcut'
        )
        shortcut_file = parents[shortcut]
        self.assertTrue(shortcut_file.tag.endswith('File'))
        self.assertEqual(shortcut_file.get('Id'), 'ServicePythonwExe')
        self.assertIsNone(shortcut.get('Target'))
        self.assertEqual(
            shortcut.get('Arguments'),
            '-I "[INSTALLFOLDER]launch.py"',
        )
        self.assertIn('Name="a&amp;b.py"', first)
        self.assertIn('Id="ServicePythonHardwareExe"', first)
        self.assertIn('Id="ServiceHardwareServiceScript"', first)
        self.assertIn('Id="ServiceShortcutRepairScript"', first)
        self.assertIn('DirectoryRef Id="SERVICEINSTALLFOLDER"', first)
        self.assertIn('DirectoryRef Id="INSTALLFOLDER"', first)
        self.assertIn('Id="ApplicationStartMenuShortcut"', first)
        self.assertIn('Advertise="yes"', first)
        self.assertIn('Icon="ProductIcon.ico"', first)
        self.assertIn('Key="System.AppUserModel.ID"', first)
        self.assertIn('Value="work.mmoe.panasonic.PostureManager"', first)
        self.assertIn(f'Name="{msi.APPLICATION_DISPLAY_NAME}"', first)
        self.assertIn(f'DisplayName="{msi.APPLICATION_DISPLAY_NAME} Hardware Service"', first)
        self.assertIn('Name="PanasonicPostureManagerHardware"', first)
        self.assertIn('Account="LocalSystem"', first)
        self.assertIn('panasonic_posture.windows.hardware_service --serve', first)
        self.assertIn('Start="install"', first)
        self.assertIn('Remove="uninstall"', first)

    def test_package_template_installs_full_runtime_and_reversible_csm_setup(self):
        source = (ROOT / 'packaging/windows/msi/Package.wxs').read_text(encoding='utf-8')
        license_text = (ROOT / 'packaging/windows/msi/LICENSE.rtf').read_text(
            encoding='utf-8'
        )
        root = ET.fromstring(source)
        self.assertTrue(root.tag.endswith('Wix'))
        self.assertIn(msi.APPLICATION_DISPLAY_NAME, license_text)
        self.assertNotIn('Panasonic Posture Manager', license_text)
        for value in (
            'Name="!(loc.ProductName)"',
            'Id="ApplicationProgramsFolder" Name="Panasonic 2-in-1 Tablet Posture Management Tool"',
            'Id="Complete" Title="!(loc.FeatureTitle)"',
            'Scope="perMachine"',
            'Id="ConfigureSlate"',
            '[#ServiceSlateConfigScript]',
            '-Action Install',
            'Id="ConfigureHardwareService"',
            '[#ServiceHardwareServiceScript]',
            '-Action Configure',
            '[System64Folder]WindowsPowerShell',
            'Id="RestoreSlate"',
            '-Action Remove',
            'Id="RollbackSlate"',
            'Id="SearchConfiguredSlate"',
            'SlateExclusiveConfigured',
            'Id="SearchWindowsBuild"',
            'Installed OR (VersionNT64 AND WINDOWS_BUILD &gt;= 10240)',
            'Id="ProgramMenuFolder"',
            'Id="ARPPRODUCTICON" Value="ProductIcon.ico"',
            'Id="DISABLEADVTSHORTCUTS" Value="1"',
            'Id="ProductIcon.ico"',
            'work.mmoe.panasonic.PostureManager.ico',
            'DllEntry="WixQuietExec"',
            'BinaryRef="Wix4UtilCA_$(sys.BUILDARCHSHORT)"',
            'Before="ConfigureSlate"',
            'Before="ConfigureHardwareService"',
            'ScheduleReboot',
            'Id="WixUI_PostureInstallDir" InstallDirectory="INSTALLFOLDER"',
            'Title="!(loc.InstallDirDialogTitle)"',
            'Id="MainPathActions"',
            'JScriptCall="CompleteAndValidateMainPath"',
            'JScriptCall="EnforceMainPath"',
            'Action="EnforceMainPath" After="CostFinalize"',
            'Id="SERVICEINSTALLFOLDER" Name="PanasonicPostureManagerService"',
            'Id="WixUI_ErrorProgressText"',
            'Id="WixUIBannerBmp"',
            'installer-banner.png',
            'Id="WixUIDialogBmp"',
            'installer-dialog.png',
            'Id="WixUIInfoIco"',
            'Id="WixUIExclamationIco"',
            'installer-info.png',
            'Id="WixUILicenseRtf" Value="$(var.LicenseRtf)"',
            'Id="LocalizeStartMenu"',
            'Id="RepairStartMenuShortcut"',
            '[#ServiceShortcutRepairScript]',
            'panasonic_posture.windows.shortcut_locale --install',
            '--language-id [UserLanguageID]',
            'Id="RemoveLocalizedStartMenu"',
            'panasonic_posture.windows.shortcut_locale --remove',
        ):
            with self.subTest(value=value):
                self.assertIn(value, source)

    def test_installer_cultures_have_complete_branding_and_copy(self):
        expected = {
            'en-US': (
                'Panasonic 2-in-1 Tablet Posture Management Tool',
                '[ProductName] Setup',
                'You may need to restart Windows',
                'Confirm install',
                'The default location works for most users. '
                'To install somewhere else, select Change.',
            ),
            'zh-CN': (
                '松下二合一平板电脑姿态管理工具',
                '[ProductName] 安装程序',
                '安装完成后可能需要重启 Windows',
                '确认安装',
                '默认位置适合大多数用户；如需安装到其他位置，请点击“更改”。',
            ),
        }
        for culture, (
            product_name,
            dialog_title,
            exit_text,
            confirm_text,
            folder_text,
        ) in expected.items():
            with self.subTest(culture=culture):
                localization, license_rtf = msi.culture_files(culture)
                root = ET.parse(localization).getroot()
                self.assertTrue(root.tag.endswith('WixLocalization'))
                self.assertEqual(root.get('Culture'), culture)
                strings = {
                    element.get('Id'): element.get('Value')
                    for element in root
                    if element.tag.endswith('String')
                }
                self.assertEqual(strings['ProductName'], product_name)
                self.assertEqual(strings['FeatureTitle'], product_name)
                self.assertEqual(strings['InstallDirDialogTitle'], dialog_title)
                self.assertIn('[ProductName]', strings['WelcomeDlgTitle'])
                self.assertNotIn(
                    'hardware service',
                    strings['WelcomeDlgDescription'].lower(),
                )
                self.assertNotIn(
                    '硬件服务',
                    strings['WelcomeDlgDescription'],
                )
                self.assertIn('InstallDirDlgTitle', strings)
                self.assertIn('InstallDirDlgDescription', strings)
                self.assertIn('InstallDirDlgFolderLabel', strings)
                self.assertIn('PostureChangeFolder', strings)
                self.assertNotIn(
                    'PanasonicPostureManager',
                    strings['InstallDirDlgFolderLabel'],
                )
                self.assertNotIn(
                    'Program Files',
                    strings['InstallDirDlgFolderLabel'],
                )
                self.assertEqual(
                    strings['InstallDirDlgFolderLabel'],
                    folder_text,
                )
                for error in (
                    'MainPathErrorFormat',
                    'MainPathErrorNetwork',
                    'MainPathErrorProtected',
                    'MainPathErrorRoot',
                    'MainPathErrorLength',
                    'MainPathErrorReparse',
                    'MainPathErrorOccupied',
                ):
                    self.assertIn(error, strings)
                self.assertIn(confirm_text, strings['VerifyReadyDlgInstall'])
                self.assertIn('[ProductName]', strings['VerifyReadyDlgInstallTitle'])
                self.assertIn(exit_text, strings['ExitDialogOptionalText'])
                self.assertNotIn(
                    'main application',
                    strings['VerifyReadyDlgInstallText'],
                )
                self.assertNotIn(
                    '主程序',
                    strings['VerifyReadyDlgInstallText'],
                )
                self.assertIn(
                    'https://www.gnu.org/licenses/agpl-3.0.html',
                    license_rtf.read_text(encoding='utf-8'),
                )

        with self.assertRaisesRegex(ValueError, 'unsupported MSI culture'):
            msi.culture_files('fr-FR')

    def test_banner_page_copy_is_right_aligned_right_of_the_brand_panel(self):
        banner_controls = {
            'BrowseDlg': ('Title', 'Description'),
            'DiskCostDlg': ('Title', 'Description'),
            'FilesInUse': ('Title', 'Description'),
            'LicenseAgreementDlg': ('Title', 'Description'),
            'MaintenanceTypeDlg': ('Title', 'Description'),
            'MsiRMFilesInUse': ('Title', 'Description'),
            'OutOfDiskDlg': ('Title', 'Description'),
            'OutOfRbDiskDlg': ('Title', 'Description'),
            'ProgressDlg': (
                'TitleInstalling',
                'TitleChanging',
                'TitleRepairing',
                'TitleRemoving',
                'TitleUpdating',
            ),
            'VerifyReadyDlg': (
                'InstallTitle',
                'ChangeTitle',
                'RepairTitle',
                'RemoveTitle',
                'UpdateTitle',
            ),
        }
        for culture in ('en-US', 'zh-CN'):
            localization, _ = msi.culture_files(culture)
            root = ET.parse(localization).getroot()
            overrides = {
                (element.get('Dialog'), element.get('Control')): element
                for element in root
                if element.tag.endswith('}UI')
            }
            for dialog, controls in banner_controls.items():
                for control in controls:
                    with self.subTest(
                        culture=culture,
                        dialog=dialog,
                        control=control,
                    ):
                        override = overrides[(dialog, control)]
                        self.assertEqual(override.get('X'), '135')
                        self.assertEqual(override.get('Width'), '220')
                        self.assertEqual(override.get('RightAligned'), 'yes')
                        if dialog in {'ProgressDlg', 'VerifyReadyDlg'}:
                            self.assertEqual(override.get('Y'), '6')
                            self.assertEqual(override.get('Height'), '32')

    def test_installer_selects_main_location_and_fixes_the_service_location(self):
        source = (ROOT / 'packaging/windows/msi/Package.wxs').read_text(
            encoding='utf-8'
        )
        root = ET.fromstring(source)
        wix_ui = next(
            element for element in root.iter()
            if element.tag.endswith('WixUI')
        )
        self.assertEqual(wix_ui.get('Id'), 'WixUI_PostureInstallDir')
        self.assertEqual(wix_ui.get('InstallDirectory'), 'INSTALLFOLDER')
        self.assertIn(
            'Id="INSTALLFOLDER" Name="PanasonicPostureManager"',
            source,
        )
        self.assertIn(
            'Id="SERVICEINSTALLFOLDER" Name="PanasonicPostureManagerService"',
            source,
        )

    def test_startup_does_not_show_the_transient_prepare_dialog(self):
        root = ET.fromstring(
            (ROOT / 'packaging/windows/msi/Package.wxs').read_text(
                encoding='utf-8'
            )
        )
        dialog_refs = {
            element.get('Id')
            for element in root.iter()
            if element.tag.endswith('DialogRef')
        }
        self.assertNotIn(
            'PrepareDlg',
            dialog_refs,
            'WixUI PrepareDlg flashes before WelcomeDlg when AppSearch is fast',
        )

    def test_launch_condition_does_not_block_maintenance_mode(self):
        root = ET.fromstring(
            (ROOT / 'packaging/windows/msi/Package.wxs').read_text(encoding='utf-8')
        )
        launch = next(
            element for element in root.iter()
            if element.tag.endswith('Launch')
        )
        self.assertEqual(
            launch.get('Condition'),
            'Installed OR (VersionNT64 AND WINDOWS_BUILD >= 10240)',
        )
        self.assertEqual(launch.get('Message'), '!(loc.LaunchConditionMessage)')

    def test_main_path_actions_complete_and_guard_the_selected_location(self):
        source = ROOT / 'packaging/windows/msi/main-path-actions.js'
        text = source.read_text(encoding='utf-8')
        self.assertNotRegex(
            text,
            r"session\.Property\([^)]*,",
            'Session.Property is a parameterized property, not a setter method',
        )
        for value in (
            "PRODUCT_FOLDER = 'PanasonicPostureManager'",
            'CompleteAndValidateMainPath',
            'EnforceMainPath',
            'DriveType !== 2',
            'GetFolder(current).Attributes & 1024',
            "'\\\\MANIFEST.json'",
            'MAIN_PATH_VALID',
            'MAIN_PATH_ERROR',
        ):
            with self.subTest(value=value):
                self.assertIn(value, text)

        cscript = shutil.which('cscript.exe')
        if sys.platform != 'win32' or cscript is None:
            self.skipTest('Windows Script Host is unavailable')
        result = subprocess.run(
            [cscript, '//nologo', '//E:JScript', str(source), '--self-test'],
            check=False,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn('self-test passed', result.stdout)

    def test_install_directory_copy_has_cjk_metrics_and_no_control_overlap(self):
        root = ET.fromstring(
            (ROOT / 'packaging/windows/msi/Package.wxs').read_text(encoding='utf-8')
        )
        ui = next(
            element for element in root.iter()
            if element.tag.endswith('}UI')
            and element.get('Id') == 'WixUI_PostureInstallDir'
        )
        styles = {
            element.get('Id'): element
            for element in ui
            if element.tag.endswith('TextStyle')
        }
        for element in styles.values():
            self.assertEqual(element.get('FaceName'), 'Microsoft YaHei UI')
        self.assertGreaterEqual(int(styles['WixUI_Font_Normal'].get('Size')), 9)

        install_dir = next(
            element for element in root.iter()
            if element.tag.endswith('Dialog')
            and element.get('Id') == 'PostureInstallDirDlg'
        )
        self.assertLessEqual(
            int(styles['WixUI_Font_Title'].get('Size')),
            9,
            'The title style must fit standard 15-DLU WiX page controls',
        )
        controls = {
            element.get('Id'): element
            for element in install_dir
            if element.tag.endswith('Control')
        }
        for control_id in ('Title', 'Description'):
            with self.subTest(control=control_id):
                self.assertEqual(controls[control_id].get('X'), '135')
                self.assertEqual(controls[control_id].get('Width'), '220')
        folder_label = controls['FolderLabel']
        folder = controls['Folder']
        change = controls['ChangeFolder']
        label_bottom = int(folder_label.get('Y')) + int(folder_label.get('Height'))
        folder_bottom = int(folder.get('Y')) + int(folder.get('Height'))
        self.assertLessEqual(label_bottom + 4, int(folder.get('Y')))
        self.assertLessEqual(folder_bottom + 4, int(change.get('Y')))
        self.assertGreaterEqual(int(folder_label.get('Height')), 34)

    def test_all_custom_banner_copy_is_right_aligned(self):
        root = ET.fromstring(
            (ROOT / 'packaging/windows/msi/Package.wxs').read_text(encoding='utf-8')
        )
        for dialog_id in ('PostureInstallDirDlg', 'InvalidMainPathDlg'):
            dialog = next(
                element for element in root.iter()
                if element.tag.endswith('Dialog')
                and element.get('Id') == dialog_id
            )
            controls = {
                element.get('Id'): element
                for element in dialog
                if element.tag.endswith('Control')
            }
            for control_id in ('Title', 'Description'):
                with self.subTest(dialog=dialog_id, control=control_id):
                    control = controls[control_id]
                    self.assertEqual(control.get('X'), '135')
                    self.assertEqual(control.get('Width'), '220')
                    self.assertEqual(control.get('RightAligned'), 'yes')

    def test_installer_branding_uses_wix_bitmap_dimensions(self):
        dimensions = {
            'installer-banner.png': (493, 58),
            'installer-dialog.png': (493, 312),
            'installer-info.png': (32, 32),
        }
        for name, expected in dimensions.items():
            with self.subTest(name=name):
                self.assertEqual(
                    png_dimensions(ROOT / 'packaging/windows/msi' / name),
                    expected,
                )

    def test_start_menu_localization_runs_after_shortcuts_are_created(self):
        root = ET.fromstring(
            (ROOT / 'packaging/windows/msi/Package.wxs').read_text(encoding='utf-8')
        )
        sequence = next(
            element for element in root.iter()
            if element.tag.endswith('InstallExecuteSequence')
        )
        localize = next(
            element for element in sequence
            if element.get('Action') == 'LocalizeStartMenu'
        )
        self.assertEqual(localize.get('After'), 'CreateShortcuts')
        self.assertIsNone(localize.get('Before'))

    @unittest.skipUnless(sys.platform == 'win32', 'MSI build runs on Windows')
    def test_failed_msi_build_preserves_the_existing_artifact(self):
        output = self.root / 'output'
        output.mkdir()
        original = output / 'panasonic-posture-manager-2.4.0-windows-x64.msi'
        original.write_bytes(b'existing')

        commands = []

        def fail(command, **options):
            commands.append(command)
            self.assertIn('WixToolset.UI.wixext', command)
            self.assertIn('WixToolset.Util.wixext', command)
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
        command = commands[0]
        self.assertIn('zh-CN', command)
        self.assertTrue(any('Setup.zh-CN.wxl' in part for part in command))
        self.assertTrue(any('LICENSE.zh-CN.rtf' in part for part in command))


if __name__ == '__main__':
    unittest.main()
