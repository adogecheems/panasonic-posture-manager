"""Build the per-machine Windows MSI from the portable application payload.

The MSI installs the same protected runtime and UI as the portable bundle,
registers the LocalSystem hardware service, and runs the reversible CSM setup
as an elevated custom action. A failed build never replaces an existing MSI.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_WXS = ROOT / 'packaging/windows/msi/Package.wxs'
WIX_NAMESPACE = 'http://wixtoolset.org/schemas/v4/wxs'
COMPONENT_NAMESPACE = uuid.UUID('2D4F313E-76E4-4E38-A886-B14075283C26')
PRODUCT_CODE_NAMESPACE = uuid.UUID('A22AA71E-57BC-40B7-AC4C-1903F752B6D5')
DEFAULT_MSI_CULTURE = 'zh-CN'
CULTURE_FILES = {
    'en-US': (
        ROOT / 'packaging/windows/msi/Setup.en-US.wxl',
        ROOT / 'packaging/windows/msi/LICENSE.rtf',
    ),
    'zh-CN': (
        ROOT / 'packaging/windows/msi/Setup.zh-CN.wxl',
        ROOT / 'packaging/windows/msi/LICENSE.zh-CN.rtf',
    ),
}

# The MSI owns service and CSM setup. Keeping the explicit helper commands out
# of Program Files prevents users and repair scripts from changing that state
# behind Windows Installer.
EXCLUDED_HELPERS = frozenset({
    'Configure Slate Device.cmd',
    'Hardware Service Status.cmd',
    'Install Hardware Service.cmd',
    'Remove Hardware Service.cmd',
    'Remove Slate Device Configuration.cmd',
    'Slate Device Status.cmd',
})

MAIN_FOLDER = 'INSTALLFOLDER'
SERVICE_FOLDER = 'SERVICEINSTALLFOLDER'
ROLE_FOLDERS = {
    'main': MAIN_FOLDER,
    'service': SERVICE_FOLDER,
}
FIXED_SERVICE_SCRIPTS = frozenset({
    'configure-slate-device.ps1',
    'hardware-service.ps1',
    'repair-start-menu-shortcut.ps1',
})
FIXED_FILE_IDS = {
    ('main', 'launch.py'): 'LaunchScript',
    ('service', 'configure-slate-device.ps1'): 'ServiceSlateConfigScript',
    ('service', 'hardware-service.ps1'): 'ServiceHardwareServiceScript',
    ('service', 'repair-start-menu-shortcut.ps1'): 'ServiceShortcutRepairScript',
    ('service', 'runtime/python-hardware.exe'): 'ServicePythonHardwareExe',
    ('service', 'runtime/pythonw.exe'): 'ServicePythonwExe',
}


def _bundle_builder():
    spec = importlib.util.spec_from_file_location(
        'windows_bundle_build', ROOT / 'packaging/windows/build.py'
    )
    if spec is None or spec.loader is None:
        raise RuntimeError('cannot load the Windows bundle builder')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


APPLICATION_DISPLAY_NAME = _bundle_builder().APPLICATION_DISPLAY_NAME


def project_version() -> str:
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
    version = str(project.get('project', {}).get('version', ''))
    if not version:
        raise ValueError('pyproject.toml has no project.version')
    return version


def _validate_test_build(test_build: int | None) -> int | None:
    if test_build is None:
        return None
    if type(test_build) is not int or not 1 <= test_build <= 65535:
        raise ValueError('test build number must be an integer from 1 to 65535')
    return test_build


def test_build_label(version: str, test_build: int) -> str:
    test_build = _validate_test_build(test_build)
    if test_build is None:
        raise ValueError('test build number is required')
    return f'{version}-build{test_build}'


def installer_version(version: str, test_build: int | None) -> str:
    """Return the numeric four-part ProductVersion used by Windows Installer."""

    test_build = _validate_test_build(test_build)
    if test_build is None:
        return version
    parts = version.split('.')
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValueError('test builds require a three-part numeric release version')
    return f'{version}.{test_build}'


def product_code(version: str, test_build: int | None = None) -> str:
    """Return one ProductCode for a formal release or installation-test build."""

    test_build = _validate_test_build(test_build)
    identity = (
        version
        if test_build is None
        else f'{version}/build{test_build}'
    )
    return str(uuid.uuid5(
        PRODUCT_CODE_NAMESPACE,
        f'panasonic-posture-manager/{identity}',
    )).upper()


def artifact_name(
    version: str,
    culture: str,
    test_build: int | None = None,
) -> str:
    """Return a culture-specific MSI name so both installers can coexist."""

    culture_files(culture)
    label = version if test_build is None else test_build_label(version, test_build)
    return f'panasonic-posture-manager-{label}-{culture}-windows-x64.msi'


def next_test_build(output: Path, version: str) -> int:
    """Return the next unused installation-test build number for a release."""

    pattern = re.compile(
        rf'^panasonic-posture-manager-{re.escape(version)}-build([1-9][0-9]*)-'
        r'(?:zh-CN|en-US)-windows-x64\.msi$'
    )
    builds = []
    if output.is_dir():
        for path in output.iterdir():
            match = pattern.fullmatch(path.name)
            if match is not None:
                builds.append(int(match.group(1)))
    build = _validate_test_build(max(builds, default=0) + 1)
    if build is None:  # pragma: no cover - the computed value is always numeric
        raise ValueError('test build number is required')
    return build


def culture_files(culture: str) -> tuple[Path, Path]:
    """Return the validation and license resources for one installer culture."""
    try:
        return CULTURE_FILES[culture]
    except KeyError as error:
        supported = ', '.join(sorted(CULTURE_FILES))
        raise ValueError(
            f'unsupported MSI culture {culture!r}; choose {supported}'
        ) from error


def _file_id(role: str, name: str) -> str:
    fixed = FIXED_FILE_IDS.get((role, name))
    if fixed is not None:
        return fixed
    digest = hashlib.sha256(f'{role}/{name}'.encode('utf-8')).hexdigest()[:20]
    return f'File_{digest}'


def _component_id(role: str, name: str) -> str:
    digest = hashlib.sha256(f'{role}/{name}'.encode('utf-8')).hexdigest()[:20]
    return f'Component_{digest}'


def _component_guid(role: str, name: str) -> str:
    return str(uuid.uuid5(
        COMPONENT_NAMESPACE,
        f'panasonic-posture-manager/{role}/{name}',
    )).upper()


def _directory_id(role: str, name: str) -> str:
    if not name:
        return ROLE_FOLDERS[role]
    digest = hashlib.sha256(f'{role}/{name}'.encode('utf-8')).hexdigest()[:20]
    return f'Directory_{digest}'


def payload_files(
    runtime: Path,
    dependencies: Path,
) -> dict[str, dict[str, Path | bytes]]:
    """Split the selectable UI from the fixed, protected service runtime."""
    portable = _bundle_builder().application_files(runtime, dependencies)
    portable['configure-slate-device.ps1'] = (
        ROOT / 'packaging/windows/configure-slate-device.ps1'
    )
    portable['hardware-service.ps1'] = (
        ROOT / 'packaging/windows/hardware-service.ps1'
    )
    portable['repair-start-menu-shortcut.ps1'] = (
        ROOT / 'packaging/windows/repair-start-menu-shortcut.ps1'
    )
    selected = {
        name: value for name, value in portable.items()
        if name not in EXCLUDED_HELPERS
        and name not in {'Hardware.cmd', f'{APPLICATION_DISPLAY_NAME}.cmd'}
    }
    main = {
        name: value for name, value in selected.items()
        if name.startswith('app/')
        or name == 'launch.py'
        or name.startswith('data/assets/')
        or name == 'po/zh_CN.po'
        or name in {'LICENSE', 'README.md', 'MAINTENANCE.md'}
    }
    service = {
        name: value for name, value in selected.items()
        if name.startswith(('runtime/', 'app/'))
        or name.startswith('data/profiles/')
        or name == 'po/zh_CN.po'
        or name in FIXED_SERVICE_SCRIPTS
    }
    required = {
        'main': {
            'app/panasonic_posture/windows/ipc/client.py',
            'launch.py',
        },
        'service': {
            'app/panasonic_posture/windows/service/daemon.py',
            'configure-slate-device.ps1',
            'hardware-service.ps1',
            'repair-start-menu-shortcut.ps1',
            'data/profiles/cfqv8.json',
            'data/profiles/cfqv9.json',
            'data/profiles/cfqv1.json',
            'po/zh_CN.po',
            'runtime/python-hardware.exe',
            'runtime/pythonw.exe',
        },
    }
    for role, names in required.items():
        files = main if role == 'main' else service
        for name in names:
            if name not in files:
                raise ValueError(f'{role} MSI payload is missing {name}')
    return {'main': main, 'service': service}


def stage_payload(
    destination: Path,
    runtime: Path,
    dependencies: Path,
) -> dict[str, dict[str, str]]:
    """Materialize both install roots and return their per-file SHA-256 manifests."""
    if destination.exists():
        raise ValueError('payload destination already exists')
    bundle = _bundle_builder()
    groups = payload_files(runtime, dependencies)
    destination.mkdir(parents=True)
    manifests = {}
    for role, groups_files in groups.items():
        role_root = destination / role
        manifest = {}
        for name, value in sorted(groups_files.items()):
            target = role_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, bytes):
                target.write_bytes(value)
            else:
                shutil.copy2(value, target)
            manifest[name] = bundle.file_digest(target)
        (role_root / 'MANIFEST.json').write_text(
            json.dumps(manifest, indent=2) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        manifests[role] = manifest
    return manifests


def _append_service(component: ET.Element) -> None:
    """Add the LocalSystem service registration to its executable component."""
    ET.SubElement(component, 'ServiceInstall', {
        'Id': 'HardwareServiceInstall',
        'Name': 'PanasonicPostureManagerHardware',
        'DisplayName': '!(loc.ServiceDisplayName)',
        'Description': '!(loc.ServiceDescription)',
        'Type': 'ownProcess',
        'Start': 'auto',
        'ErrorControl': 'normal',
        'Account': 'LocalSystem',
        'Vital': 'yes',
        'Arguments': '-B -I -m panasonic_posture.windows.service.daemon --serve',
    })
    ET.SubElement(component, 'ServiceControl', {
        'Id': 'HardwareServiceControl',
        'Name': 'PanasonicPostureManagerHardware',
        'Start': 'install',
        'Stop': 'uninstall',
        'Remove': 'uninstall',
        'Wait': 'yes',
    })


def _append_shortcut(file_element: ET.Element) -> None:
    """Attach the advertised Start menu shortcut to the UI executable."""
    shortcut = ET.SubElement(file_element, 'Shortcut', {
        'Id': 'ApplicationStartMenuShortcut',
        'Directory': 'ProgramMenuFolder',
        'Name': APPLICATION_DISPLAY_NAME,
        'Description': '!(loc.ShortcutDescription)',
        'Advertise': 'yes',
        'Icon': 'ProductIcon.ico',
        'Arguments': '-B -I "[INSTALLFOLDER]launch.py"',
        'WorkingDirectory': 'INSTALLFOLDER',
    })
    ET.SubElement(shortcut, 'ShortcutProperty', {
        'Key': 'System.AppUserModel.ID',
        'Value': 'work.mmoe.panasonic.PostureManager',
    })


def render_components(
    groups: dict[str, dict[str, Path | bytes]],
    *,
    source_for=None,
) -> str:
    """Create deterministic WiX components for the main and service roots."""
    source_for = source_for or (
        lambda role, name: (
            '$(var.PayloadDir)\\'
            + role
            + '\\'
            + name.replace('/', '\\')
        )
    )

    root = ET.Element('Wix', {'xmlns': WIX_NAMESPACE})
    component_ids: list[str] = []
    for role in ('main', 'service'):
        files = groups[role]
        children: dict[str, set[str]] = defaultdict(set)
        for name in files:
            path = PurePosixPath(name)
            parent = ''
            for part in path.parts[:-1]:
                child = f'{parent}/{part}' if parent else part
                children[parent].add(child)
                parent = child

        fragment = ET.SubElement(root, 'Fragment')
        directory_ref = ET.SubElement(fragment, 'DirectoryRef', {
            'Id': ROLE_FOLDERS[role],
        })

        def write_directories(parent: str, element: ET.Element) -> None:
            for relative in sorted(children[parent]):
                directory = ET.SubElement(element, 'Directory', {
                    'Id': _directory_id(role, relative),
                    'Name': PurePosixPath(relative).name,
                })
                write_directories(relative, directory)

        write_directories('', directory_ref)
        for name in sorted(files):
            path = PurePosixPath(name)
            parent = str(path.parent) if str(path.parent) != '.' else ''
            component = _component_id(role, name)
            component_ids.append(component)
            component_element = ET.SubElement(fragment, 'Component', {
                'Id': component,
                'Directory': _directory_id(role, parent),
                'Guid': _component_guid(role, name),
                'Bitness': 'always64',
            })
            source = str(source_for(role, name))
            file_element = ET.SubElement(component_element, 'File', {
                'Id': _file_id(role, name),
                'Name': path.name,
                'Source': source,
                'KeyPath': 'yes',
            })
            if role == 'service' and name == 'runtime/pythonw.exe':
                _append_shortcut(file_element)
            if role == 'service' and name == 'runtime/python-hardware.exe':
                _append_service(component_element)

    group_fragment = ET.SubElement(root, 'Fragment')
    group = ET.SubElement(group_fragment, 'ComponentGroup', {
        'Id': 'PayloadComponents',
    })
    for component in component_ids:
        ET.SubElement(group, 'ComponentRef', {'Id': component})
    ET.indent(root, space='  ')
    return ET.tostring(root, encoding='unicode') + '\n'


def generate_components(destination: Path, runtime: Path, dependencies: Path) -> Path:
    """Stage the payload beside a generated WiX component fragment."""
    payload = destination / 'payload'
    stage_payload(payload, runtime, dependencies)
    groups = payload_files(runtime, dependencies)
    components = destination / 'Components.wxs'
    components.write_text(render_components(groups), encoding='utf-8', newline='\n')
    return components


def build(
    output: Path,
    runtime: Path,
    dependencies: Path,
    *,
    version: str | None = None,
    culture: str = DEFAULT_MSI_CULTURE,
    test_build: int | None = None,
    wix: str = 'wix',
    runner=subprocess.run,
) -> Path:
    """Build one versioned MSI atomically and return its path."""
    version = version or project_version()
    if version != project_version():
        raise ValueError(f'MSI version {version!r} does not match the project version')
    test_build = _validate_test_build(test_build)
    if not PACKAGE_WXS.is_file():
        raise ValueError('MSI package source is missing')
    if sys.platform != 'win32':
        raise OSError('the Windows MSI build must run on Windows')
    localization, license_rtf = culture_files(culture)
    for resource in (localization, license_rtf):
        if not resource.is_file():
            raise ValueError(f'MSI culture resource is missing: {resource}')

    output.mkdir(parents=True, exist_ok=True)
    archive = output / artifact_name(version, culture, test_build)
    with tempfile.TemporaryDirectory(prefix='posture-msi-', dir=output) as temporary:
        work = Path(temporary)
        groups = payload_files(runtime, dependencies)
        if test_build is not None:
            label = test_build_label(version, test_build)
            for role in groups:
                groups[role]['app/panasonic_posture/BUILD-VERSION'] = (
                    f'{label}\n'.encode('ascii')
                )
        generated = work / 'generated'
        generated.mkdir()
        sources = {}
        bundle = _bundle_builder()
        for role, files in groups.items():
            manifest = {}
            for name, value in sorted(files.items()):
                if isinstance(value, bytes):
                    source = generated / role / name
                    source.parent.mkdir(parents=True, exist_ok=True)
                    source.write_bytes(value)
                    manifest[name] = hashlib.sha256(value).hexdigest()
                else:
                    source = Path(value).absolute()
                    manifest[name] = bundle.file_digest(source)
                sources[(role, name)] = source
            manifest_path = generated / role / 'MANIFEST.json'
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(
                json.dumps(manifest, indent=2) + '\n',
                encoding='utf-8',
                newline='\n',
            )
            groups[role]['MANIFEST.json'] = manifest_path
            sources[(role, 'MANIFEST.json')] = manifest_path
        components = work / 'Components.wxs'
        components.write_text(
            render_components(
                groups,
                source_for=lambda role, name: sources[(role, name)],
            ),
            encoding='utf-8',
            newline='\n',
        )
        temporary_msi = work / archive.name
        runner([
            wix, 'build',
            '-arch', 'x64',
            '-culture', culture,
            '-ext', 'WixToolset.UI.wixext',
            '-ext', 'WixToolset.Util.wixext',
            '-loc', str(localization),
            '-d', f'ProductVersion={installer_version(version, test_build)}',
            '-d', f'ProductCode={product_code(version, test_build)}',
            '-d', f'ProjectRoot={ROOT}',
            '-d', f'LicenseRtf={license_rtf}',
            '-o', str(temporary_msi),
            str(PACKAGE_WXS),
            str(components),
        ], cwd=ROOT, check=True)
        if not temporary_msi.is_file():
            raise OSError('WiX did not create the requested MSI')
        runner([
            wix, 'msi', 'validate',
            '-sice', 'ICE61',  # Same-version rebuilds are an explicit release mode.
            str(temporary_msi),
        ], cwd=ROOT, check=True)
        temporary_msi.replace(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix('.msi.sha256').write_text(
        f'{digest}  {archive.name}\n', encoding='ascii'
    )
    return archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(sys.base_prefix))
    parser.add_argument('--dependencies', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/windows')
    parser.add_argument('--version', default=None)
    parser.add_argument(
        '--culture',
        choices=tuple(sorted(CULTURE_FILES)),
        default=DEFAULT_MSI_CULTURE,
        help='Windows Installer UI culture (default: zh-CN)',
    )
    parser.add_argument(
        '--test-build',
        nargs='?',
        const=0,
        type=int,
        metavar='N',
        help='build installation-test MSI as VERSION-buildN; omit N for the next number',
    )
    parser.add_argument('--wix', default='wix')
    options = parser.parse_args(argv)
    test_build = options.test_build
    if test_build == 0:
        try:
            test_build = next_test_build(options.output, project_version())
        except ValueError as error:
            parser.error(str(error))
    elif test_build is not None:
        try:
            test_build = _validate_test_build(test_build)
        except ValueError as error:
            parser.error(str(error))
    print(build(
        options.output,
        options.runtime,
        options.dependencies,
        version=options.version,
        culture=options.culture,
        test_build=test_build,
        wix=options.wix,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
