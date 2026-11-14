"""Windows MSI payload, service registration and release-source contracts."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from panasonic_posture.windows.app.shortcut_locale import (
    START_MENU_RELATIVE_PATH,
    remove_start_menu_entries,
)

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
        for name in (
            'win32/win32api.pyd',
            'win32/win32gui.pyd',
            'win32/win32print.pyd',
            'win32/lib/win32con.py',
            'win32/lib/pywin32_bootstrap.py',
            'win32comext/shell/__init__.py',
            'win32comext/shell/shell.pyd',
            'pywin32.pth',
            'pywin32_system32/pywintypes312.dll',
            'pywin32_system32/pythoncom312.dll',
        ):
            path = self.dependencies / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'pywin32')
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
        self.assertIn('app/panasonic_posture/windows/ipc/client.py', main)
        self.assertNotIn('runtime/python-hardware.exe', main)
        self.assertIn('runtime/python-hardware.exe', service)
        self.assertIn('runtime/python-hardware._pth', service)
        self.assertIn('app/panasonic_posture/windows/service/daemon.py', service)
        self.assertIn('configure-slate-device.ps1', service)
        self.assertIn('hardware-service.ps1', service)
        self.assertIn('data/profiles/cfqv8.json', service)
        self.assertIn('data/profiles/cfqv9.json', service)
        self.assertIn('data/profiles/cfqv1.json', service)
        self.assertIn('po/zh_CN.po', service)
        self.assertNotIn('data/profiles/cfqv8.json', main)
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

    def test_service_payload_can_remove_a_localized_start_menu_entry(self):
        payload = self.root / 'payload'
        msi.stage_payload(payload, self.runtime, self.dependencies)
        program_data = self.root / 'ProgramData'
        programs = program_data / START_MENU_RELATIVE_PATH
        localized = programs / '松下二合一平板电脑姿态管理工具'
        localized.mkdir(parents=True)
        shortcut = localized / '松下二合一平板电脑姿态管理工具.lnk'
        shortcut.write_bytes(b'shortcut')

        with patch.dict(
            os.environ,
            {'PANASONIC_POSTURE_DATA_DIR': str(payload / 'service/data')},
        ):
            remove_start_menu_entries(program_data)

        self.assertFalse(shortcut.exists())
        self.assertFalse(localized.exists())

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
        self.assertEqual(shortcut.get('Directory'), 'ProgramMenuFolder')
        self.assertEqual(
            shortcut.get('Arguments'),
            '-B -I "[INSTALLFOLDER]launch.py"',
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
        self.assertNotIn('ApplicationProgramsFolder', first)
        self.assertNotIn('RemoveFolder', first)
        self.assertIn('Key="System.AppUserModel.ID"', first)
        self.assertIn('Value="work.mmoe.panasonic.PostureManager"', first)
        self.assertIn(f'Name="{msi.APPLICATION_DISPLAY_NAME}"', first)
        self.assertEqual(shortcut.get('Description'), '!(loc.ShortcutDescription)')
        service = next(
            element for element in root.iter()
            if element.tag.endswith('ServiceInstall')
        )
        self.assertEqual(service.get('DisplayName'), '!(loc.ServiceDisplayName)')
        self.assertEqual(service.get('Description'), '!(loc.ServiceDescription)')
        self.assertIn('Name="PanasonicPostureManagerHardware"', first)
        self.assertIn('Account="LocalSystem"', first)
        self.assertIn('panasonic_posture.windows.service.daemon --serve', first)
        control = next(
            element for element in root.iter()
            if element.tag.endswith('ServiceControl')
        )
        self.assertIsNone(control.get('Start'))
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
            'ProductCode="$(var.ProductCode)"',
            'UpgradeCode="7C0319E4-768D-4C48-A41C-25F0D677EA89"',
            '<MajorUpgrade',
            'Name="!(loc.ProductName)"',
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
            'panasonic_posture.windows.app.shortcut_locale --install',
            '--language-id [UserLanguageID]',
            'Id="RemoveLocalizedStartMenu"',
            'panasonic_posture.windows.app.shortcut_locale --remove',
            'Id="WIXUI_EXITDIALOGOPTIONALTEXT"',
            'Before="ExecuteAction"',
            'Sequence="ui"',
            'Id="POSTURE_EXIT_TITLE"',
            'Id="POSTURE_EXIT_DESCRIPTION"',
        ):
            with self.subTest(value=value):
                self.assertIn(value, source)
        self.assertNotIn('ApplicationProgramsFolder', source)

    def test_installer_cultures_have_complete_branding_and_copy(self):
        expected = {
            'en-US': (
                'Panasonic 2-in-1 Tablet Posture Management Tool',
                '[ProductName] Setup',
                'You may need to restart Windows',
                'Confirm install',
                'The default location works for most users. '
                'To install somewhere else, select Change.',
                'Open [ProductName]',
                '[ProductName] Hardware Service',
                'Provides the authenticated LocalSystem hardware endpoint for [ProductName].',
            ),
            'zh-CN': (
                '松下二合一平板电脑姿态管理工具',
                '[ProductName] 安装程序',
                '安装完成后可能需要重启 Windows',
                '确认安装',
                '默认位置适合大多数用户；如需安装到其他位置，请点击“更改”。',
                '打开 [ProductName]',
                '[ProductName] 硬件服务',
                '为 [ProductName] 提供经过身份验证的 LocalSystem 硬件端点。',
            ),
        }
        for culture, (
            product_name,
            dialog_title,
            exit_text,
            confirm_text,
            folder_text,
            shortcut_description,
            service_display_name,
            service_description,
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
                self.assertEqual(
                    strings['ShortcutDescription'],
                    shortcut_description,
                )
                self.assertEqual(
                    strings['ServiceDisplayName'],
                    service_display_name,
                )
                self.assertEqual(
                    strings['ServiceDescription'],
                    service_description,
                )
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

    def test_uninstall_uses_branded_installer_flow_and_copy(self):
        source = (ROOT / 'packaging/windows/msi/Package.wxs').read_text(
            encoding='utf-8'
        )
        root = ET.fromstring(source)
        exit_text = next(
            element for element in root.iter()
            if element.tag.endswith('SetProperty')
            and element.get('Id') == 'WIXUI_EXITDIALOGOPTIONALTEXT'
        )
        self.assertEqual(exit_text.get('Before'), 'ExecuteAction')
        self.assertEqual(exit_text.get('Sequence'), 'ui')
        self.assertEqual(
            exit_text.get('Value'),
            '!(loc.ExitDialogRemoveOptionalText)',
        )
        self.assertIn('WixUI_InstallMode = "Remove"', exit_text.get('Condition'))
        self.assertIn('REMOVE~="ALL"', exit_text.get('Condition'))
        for property_id in ('POSTURE_EXIT_TITLE', 'POSTURE_EXIT_DESCRIPTION'):
            action = next(
                element for element in root.iter()
                if element.tag.endswith('SetProperty')
                and element.get('Id') == property_id
            )
            self.assertEqual(action.get('Before'), 'ExecuteAction')
            self.assertEqual(action.get('Sequence'), 'ui')
            self.assertIn('WixUI_InstallMode = "Remove"', action.get('Condition'))
            self.assertIn('REMOVE~="ALL"', action.get('Condition'))

        expected = {
            'en-US': (
                'Panasonic 2-in-1 Tablet Posture Management Tool',
                'Maintain or uninstall [ProductName]',
                'Uninstall [ProductName]',
                'Uninstalling [ProductName]',
                'Select Uninstall',
                'Uninstallation is complete',
                'hardware service and restoring display',
            ),
            'zh-CN': (
                '松下二合一平板电脑姿态管理工具',
                '维护或卸载 [ProductName]',
                '确认卸载 [ProductName]',
                '正在卸载 [ProductName]',
                '点击“卸载”',
                '卸载完成',
                '停止硬件服务并恢复显示',
            ),
        }
        for culture, (product_name, *fragments) in expected.items():
            with self.subTest(culture=culture):
                localization, _ = msi.culture_files(culture)
                root = ET.parse(localization).getroot()
                strings = {
                    element.get('Id'): element.get('Value')
                    for element in root
                    if element.tag.endswith('String')
                }
                for string_id, fragment in zip(
                    (
                        'MaintenanceWelcomeDlgTitle',
                        'VerifyReadyDlgRemoveTitle',
                        'ProgressDlgTitleRemoving',
                        'VerifyReadyDlgRemoveText',
                        'ExitDialogRemoveOptionalText',
                        'ProgressDlgTextRemoving',
                    ),
                    fragments,
                ):
                    with self.subTest(string=string_id):
                        self.assertIn(fragment, strings[string_id])
                self.assertNotEqual(
                    strings['ExitDialogOptionalText'],
                    strings['ExitDialogRemoveOptionalText'],
                )
                self.assertNotEqual(
                    strings['ExitDialogInstallTitle'],
                    strings['ExitDialogRemoveTitle'],
                )
                for string_id in (
                    'ExitDialogInstallTitle',
                    'ExitDialogRemoveTitle',
                ):
                    self.assertIn(product_name, strings[string_id])
                    self.assertNotIn('[ProductName]', strings[string_id])
                self.assertIn('[ProductName]', strings['MaintenanceTypeDlgDescription'])
                self.assertIn(
                    '卸载' if culture == 'zh-CN' else 'Uninstall',
                    strings['MaintenanceTypeDlgRemoveButton'],
                )
                overrides = {
                    (element.get('Dialog'), element.get('Control')): element
                    for element in root
                    if element.tag.endswith('}UI')
                }
                self.assertEqual(
                    overrides[('ExitDialog', 'Title')].get('Text'),
                    '[POSTURE_EXIT_TITLE]',
                )
                self.assertEqual(
                    overrides[('ExitDialog', 'Description')].get('Text'),
                    '[POSTURE_EXIT_DESCRIPTION]',
                )

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
        self.assertEqual(
            styles['WixUI_Font_Title'].get('Bold'),
            'no',
            'Page titles must use the same regular weight as the welcome heading',
        )

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

    def test_shortcut_repair_uses_the_programs_root_and_migrates_old_folders(self):
        source = (
            ROOT / 'packaging/windows/repair-start-menu-shortcut.ps1'
        ).read_text(encoding='utf-8')
        self.assertIn(
            '$destinationLink = Join-Path $programs "$displayName.lnk"',
            source,
        )
        self.assertNotIn('$destinationFolder', source)
        self.assertNotIn('New-Item -ItemType Directory', source)
        self.assertIn('$folder = Join-Path $programs $name', source)

    @unittest.skipUnless(sys.platform == 'win32', 'MSI build runs on Windows')
    def test_failed_msi_build_preserves_the_existing_artifact(self):
        version = msi.project_version()
        output = self.root / 'output'
        output.mkdir()
        original = output / msi.artifact_name(version, 'zh-CN')
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
                version=version,
                runner=fail,
            )
        self.assertEqual(original.read_bytes(), b'existing')
        command = commands[0]
        self.assertIn('zh-CN', command)
        self.assertIn(f'ProductCode={msi.product_code(version)}', command)
        self.assertTrue(any('Setup.zh-CN.wxl' in part for part in command))
        self.assertTrue(any('LICENSE.zh-CN.rtf' in part for part in command))
        self.assertEqual(
            msi.artifact_name(version, 'zh-CN'),
            f'panasonic-posture-manager-{version}-zh-CN-windows-x64.msi',
        )
        self.assertEqual(
            msi.artifact_name(version, 'en-US'),
            f'panasonic-posture-manager-{version}-en-US-windows-x64.msi',
        )
        self.assertEqual(
            msi.product_code('2.4.0'),
            msi.product_code('2.4.0'),
        )
        self.assertNotEqual(
            msi.product_code('2.4.0'),
            msi.product_code('2.4.1'),
        )

    def test_installation_test_builds_have_a_visible_suffix(self):
        self.assertEqual(
            msi.artifact_name('2.4.0', 'zh-CN', test_build=7),
            'panasonic-posture-manager-2.4.0-build7-zh-CN-windows-x64.msi',
        )
        self.assertEqual(msi.test_build_label('2.4.0', 7), '2.4.0-build7')
        self.assertEqual(msi.installer_version('2.4.0', 7), '2.4.0.7')
        self.assertEqual(
            msi.product_code('2.4.0'),
            msi.product_code('2.4.0'),
        )
        self.assertEqual(
            msi.product_code('2.4.0', test_build=7),
            msi.product_code('2.4.0', test_build=7),
        )
        self.assertNotEqual(
            msi.product_code('2.4.0'),
            msi.product_code('2.4.0', test_build=7),
        )
        self.assertNotEqual(
            msi.product_code('2.4.0', test_build=7),
            msi.product_code('2.4.0', test_build=8),
        )
        for invalid in (0, -1, 65536, True, '7'):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    msi.test_build_label('2.4.0', invalid)
                with self.assertRaises(ValueError):
                    msi.product_code('2.4.0', test_build=invalid)

    def test_next_installation_test_build_ignores_other_releases(self):
        output = self.root / 'numbered-output'
        output.mkdir()
        for name in (
            msi.artifact_name('2.4.0', 'zh-CN', test_build=2),
            msi.artifact_name('2.4.0', 'en-US', test_build=5),
            msi.artifact_name('2.4.0', 'zh-CN'),
            msi.artifact_name('2.4.1', 'zh-CN', test_build=20),
        ):
            (output / name).write_bytes(b'msi')
        self.assertEqual(msi.next_test_build(output, '2.4.0'), 6)

    def test_test_build_injects_label_and_numeric_product_version(self):
        version = msi.project_version()
        output = self.root / 'test-build-output'
        commands = []

        def build_with_fixture(command, **options):
            commands.append(command)
            if command[0] == 'wix-fixture' and command[1] == 'build':
                components = Path(command[-1]).read_text(encoding='utf-8')
                root = ET.fromstring(components)
                sources = [
                    element.get('Source')
                    for element in root.iter()
                    if element.tag.endswith('File')
                    and (element.get('Source') or '').endswith('BUILD-VERSION')
                ]
                self.assertEqual(len(sources), 2)
                self.assertTrue(all(
                    Path(source).read_text(encoding='ascii')
                    == f'{version}-build7\n'
                    for source in sources
                ))
                destination = Path(command[command.index('-o') + 1])
                destination.write_bytes(b'msi fixture')
            return None

        package = msi.build(
            output,
            self.runtime,
            self.dependencies,
            version=version,
            culture='zh-CN',
            test_build=7,
            wix='wix-fixture',
            runner=build_with_fixture,
        )
        self.assertEqual(package.name,
                         f'panasonic-posture-manager-{version}-build7-'
                         'zh-CN-windows-x64.msi')
        compile_command = commands[0]
        self.assertIn(f'ProductVersion={version}.7', compile_command)
        self.assertIn(
            f'ProductCode={msi.product_code(version, test_build=7)}',
            compile_command,
        )

    def test_both_cultures_build_with_distinct_names_and_one_product_identity(self):
        version = msi.project_version()
        output = self.root / 'localized-output'
        commands = []

        def build_with_fixture(command, **options):
            commands.append(command)
            if 'build' in command:
                destination = Path(command[command.index('-o') + 1])
                destination.write_bytes('msi fixture'.encode('utf-8'))
            return None

        packages = [
            msi.build(
                output,
                self.runtime,
                self.dependencies,
                version=version,
                culture=culture,
                test_build=7,
                wix='wix-fixture',
                runner=build_with_fixture,
            )
            for culture in ('zh-CN', 'en-US')
        ]
        self.assertEqual(
            [package.name for package in packages],
            [
                f'panasonic-posture-manager-{version}-build7-'
                'zh-CN-windows-x64.msi',
                f'panasonic-posture-manager-{version}-build7-'
                'en-US-windows-x64.msi',
            ],
        )
        build_commands = [command for command in commands if 'build' in command]
        self.assertEqual(len(build_commands), 2)
        for command in build_commands:
            self.assertIn(
                f'ProductCode={msi.product_code(version, test_build=7)}',
                command,
            )
            self.assertIn(f'ProductVersion={version}.7', command)
        self.assertNotEqual(
            build_commands[0][build_commands[0].index('-culture') + 1],
            build_commands[1][build_commands[1].index('-culture') + 1],
        )


if __name__ == '__main__':
    unittest.main()
