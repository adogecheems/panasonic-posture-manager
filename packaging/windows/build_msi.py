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
import shutil
import subprocess
import sys
import tempfile
import tomllib
import uuid
from xml.sax.saxutils import quoteattr

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_WXS = ROOT / 'packaging/windows/msi/Package.wxs'
COMPONENT_NAMESPACE = uuid.UUID('2D4F313E-76E4-4E38-A886-B14075283C26')
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
        or name in FIXED_SERVICE_SCRIPTS
    }
    required = {
        'main': {
            'app/panasonic_posture/windows/hardware_client.py',
            'launch.py',
        },
        'service': {
            'app/panasonic_posture/windows/hardware_service.py',
            'configure-slate-device.ps1',
            'hardware-service.ps1',
            'repair-start-menu-shortcut.ps1',
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


def _attribute(value: object) -> str:
    return quoteattr(str(value))


def _service_xml() -> str:
    return f'''
      <ServiceInstall
          Id="HardwareServiceInstall"
          Name="PanasonicPostureManagerHardware"
          DisplayName="{APPLICATION_DISPLAY_NAME} Hardware Service"
          Description="Provides the authenticated LocalSystem hardware endpoint for {APPLICATION_DISPLAY_NAME}."
          Type="ownProcess"
          Start="auto"
          ErrorControl="normal"
          Account="LocalSystem"
          Vital="yes"
          Arguments="-B -I -m panasonic_posture.windows.hardware_service --serve" />
      <ServiceControl
          Id="HardwareServiceControl"
          Name="PanasonicPostureManagerHardware"
          Start="install"
          Stop="uninstall"
          Remove="uninstall"
          Wait="yes" />'''


def _shortcut_xml() -> str:
    return f'''
          <Shortcut
              Id="ApplicationStartMenuShortcut"
              Directory="ApplicationProgramsFolder"
              Name="{APPLICATION_DISPLAY_NAME}"
              Description="Open {APPLICATION_DISPLAY_NAME}"
              Advertise="yes"
              Icon="ProductIcon.ico"
              Arguments="-I &quot;[INSTALLFOLDER]launch.py&quot;"
              WorkingDirectory="INSTALLFOLDER">
            <ShortcutProperty
                Key="System.AppUserModel.ID"
                Value="work.mmoe.panasonic.PostureManager" />
          </Shortcut>'''


def _shortcut_remove_xml() -> str:
    return '''
        <RemoveFolder
            Id="RemoveApplicationProgramsFolder"
            Directory="ApplicationProgramsFolder"
            On="uninstall" />'''


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

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">',
    ]
    component_ids = []
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

        lines.extend((
            '  <Fragment>',
            f'    <DirectoryRef Id="{ROLE_FOLDERS[role]}">',
        ))

        def write_directories(parent: str, indent: int) -> None:
            for relative in sorted(children[parent]):
                padding = ' ' * indent
                lines.append(
                    f'{padding}'
                    f'<Directory Id={_attribute(_directory_id(role, relative))} '
                    f'Name={_attribute(PurePosixPath(relative).name)}>'
                )
                write_directories(relative, indent + 2)
                lines.append(f'{padding}</Directory>')

        write_directories('', 6)
        lines.append('    </DirectoryRef>')
        for name in sorted(files):
            path = PurePosixPath(name)
            parent = str(path.parent) if str(path.parent) != '.' else ''
            component = _component_id(role, name)
            component_ids.append(component)
            lines.append(
                f'    <Component Id={_attribute(component)} '
                f'Directory={_attribute(_directory_id(role, parent))} '
                f'Guid={_attribute(_component_guid(role, name))} Bitness="always64">'
            )
            source = str(source_for(role, name))
            file_xml = (
                f'      <File Id={_attribute(_file_id(role, name))} '
                f'Name={_attribute(path.name)} Source={_attribute(source)} KeyPath="yes"'
            )
            if role == 'service' and name == 'runtime/pythonw.exe':
                lines.append(file_xml + '>')
                lines.append(_shortcut_xml())
                lines.append('      </File>')
                lines.append(_shortcut_remove_xml())
            else:
                lines.append(file_xml + ' />')
            if role == 'service' and name == 'runtime/python-hardware.exe':
                lines.append(_service_xml())
            lines.append('    </Component>')
        lines.append('  </Fragment>')
    lines.extend(('  <Fragment>', '    <ComponentGroup Id="PayloadComponents">'))
    for component in component_ids:
        lines.append(f'      <ComponentRef Id={_attribute(component)} />')
    lines.extend(('    </ComponentGroup>', '  </Fragment>', '</Wix>', ''))
    return '\n'.join(lines)


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
    wix: str = 'wix',
    runner=subprocess.run,
) -> Path:
    """Build one versioned MSI atomically and return its path."""
    version = version or project_version()
    if version != project_version():
        raise ValueError(f'MSI version {version!r} does not match the project version')
    if not PACKAGE_WXS.is_file():
        raise ValueError('MSI package source is missing')
    if sys.platform != 'win32':
        raise OSError('the Windows MSI build must run on Windows')
    localization, license_rtf = culture_files(culture)
    for resource in (localization, license_rtf):
        if not resource.is_file():
            raise ValueError(f'MSI culture resource is missing: {resource}')

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'panasonic-posture-manager-{version}-windows-x64.msi'
    with tempfile.TemporaryDirectory(prefix='posture-msi-', dir=output) as temporary:
        work = Path(temporary)
        groups = payload_files(runtime, dependencies)
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
            '-d', f'ProductVersion={version}',
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
    parser.add_argument('--wix', default='wix')
    options = parser.parse_args(argv)
    print(build(
        options.output,
        options.runtime,
        options.dependencies,
        version=options.version,
        culture=options.culture,
        wix=options.wix,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
