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

FIXED_FILE_IDS = {
    'configure-slate-device.ps1': 'SlateConfigScript',
    'hardware-service.ps1': 'HardwareServiceScript',
    'launch.py': 'LaunchScript',
    'runtime/python-hardware.exe': 'PythonHardwareExe',
    'runtime/pythonw.exe': 'PythonwExe',
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


def _file_id(name: str) -> str:
    fixed = FIXED_FILE_IDS.get(name)
    if fixed is not None:
        return fixed
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:20]
    return f'File_{digest}'


def _component_id(name: str) -> str:
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:20]
    return f'Component_{digest}'


def _component_guid(name: str) -> str:
    return str(uuid.uuid5(COMPONENT_NAMESPACE, f'panasonic-posture-manager/{name}')).upper()


def _directory_id(name: str) -> str:
    if not name:
        return 'INSTALLFOLDER'
    digest = hashlib.sha256(name.encode('utf-8')).hexdigest()[:20]
    return f'Directory_{digest}'


def payload_files(runtime: Path, dependencies: Path) -> dict[str, Path | bytes]:
    files = _bundle_builder().application_files(runtime, dependencies)
    files['configure-slate-device.ps1'] = (
        ROOT / 'packaging/windows/configure-slate-device.ps1'
    )
    files['hardware-service.ps1'] = (
        ROOT / 'packaging/windows/hardware-service.ps1'
    )
    selected = {
        name: value for name, value in files.items()
        if name not in EXCLUDED_HELPERS
    }
    for required in FIXED_FILE_IDS:
        if required not in selected:
            raise ValueError(f'MSI payload is missing {required}')
    return selected


def stage_payload(
    destination: Path,
    runtime: Path,
    dependencies: Path,
) -> dict[str, str]:
    """Materialize the MSI payload and return its per-file SHA-256 manifest."""
    if destination.exists():
        raise ValueError('payload destination already exists')
    bundle = _bundle_builder()
    files = payload_files(runtime, dependencies)
    destination.mkdir(parents=True)
    manifest = {}
    for name, value in sorted(files.items()):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            target.write_bytes(value)
        else:
            shutil.copy2(value, target)
        manifest[name] = bundle.file_digest(target)
    (destination / 'MANIFEST.json').write_text(
        json.dumps(manifest, indent=2) + '\n', encoding='utf-8', newline='\n'
    )
    return manifest


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
        </Shortcut>
        <RemoveFolder
            Id="RemoveApplicationProgramsFolder"
            Directory="ApplicationProgramsFolder"
            On="uninstall" />'''


def render_components(
    files: dict[str, Path | bytes],
    *,
    source_for=None,
) -> str:
    """Create deterministic WiX source for the complete payload tree."""
    source_for = source_for or (
        lambda name: '$(var.PayloadDir)\\' + name.replace('/', '\\')
    )
    children: dict[str, set[str]] = defaultdict(set)
    paths = [PurePosixPath(name) for name in files]
    for path in paths:
        parent = ''
        for part in path.parts[:-1]:
            child = f'{parent}/{part}' if parent else part
            children[parent].add(child)
            parent = child

    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">',
        '  <Fragment>',
        '    <DirectoryRef Id="INSTALLFOLDER">',
    ]

    def write_directories(parent: str, indent: int) -> None:
        for relative in sorted(children[parent]):
            padding = ' ' * indent
            lines.append(
                f'{padding}<Directory Id={_attribute(_directory_id(relative))} '
                f'Name={_attribute(PurePosixPath(relative).name)}>'
            )
            write_directories(relative, indent + 2)
            lines.append(f'{padding}</Directory>')

    write_directories('', 6)
    lines.extend(('    </DirectoryRef>', '  </Fragment>', '  <Fragment>',
                  '    <ComponentGroup Id="PayloadComponents">'))
    for name in sorted(files):
        path = PurePosixPath(name)
        parent = str(path.parent) if str(path.parent) != '.' else ''
        lines.append(
            f'      <Component Id={_attribute(_component_id(name))} '
            f'Directory={_attribute(_directory_id(parent))} '
            f'Guid={_attribute(_component_guid(name))} Bitness="always64">'
        )
        source = str(source_for(name))
        lines.append(
            f'        <File Id={_attribute(_file_id(name))} '
            f'Name={_attribute(path.name)} Source={_attribute(source)} KeyPath="yes" />'
        )
        if name == 'runtime/python-hardware.exe':
            lines.append(_service_xml())
        if name == 'runtime/pythonw.exe':
            lines.append(_shortcut_xml())
        lines.append('      </Component>')
    lines.extend(('    </ComponentGroup>', '  </Fragment>', '</Wix>', ''))
    return '\n'.join(lines)


def generate_components(destination: Path, runtime: Path, dependencies: Path) -> Path:
    """Stage the payload beside a generated WiX component fragment."""
    payload = destination / 'payload'
    stage_payload(payload, runtime, dependencies)
    files = payload_files(runtime, dependencies)
    components = destination / 'Components.wxs'
    components.write_text(render_components(files), encoding='utf-8', newline='\n')
    return components


def build(
    output: Path,
    runtime: Path,
    dependencies: Path,
    *,
    version: str | None = None,
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

    output.mkdir(parents=True, exist_ok=True)
    archive = output / f'panasonic-posture-manager-{version}-windows-x64.msi'
    with tempfile.TemporaryDirectory(prefix='posture-msi-', dir=output) as temporary:
        work = Path(temporary)
        files = payload_files(runtime, dependencies)
        generated = work / 'generated'
        generated.mkdir()
        manifest = {}
        sources = {}
        bundle = _bundle_builder()
        for name, value in sorted(files.items()):
            if isinstance(value, bytes):
                source = generated / name
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_bytes(value)
                manifest[name] = hashlib.sha256(value).hexdigest()
            else:
                source = Path(value).absolute()
                manifest[name] = bundle.file_digest(source)
            sources[name] = source
        manifest_path = work / 'MANIFEST.json'
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + '\n',
            encoding='utf-8',
            newline='\n',
        )
        sources['MANIFEST.json'] = manifest_path
        components = work / 'Components.wxs'
        components.write_text(
            render_components(sources, source_for=sources.__getitem__),
            encoding='utf-8',
            newline='\n',
        )
        temporary_msi = work / archive.name
        runner([
            wix, 'build',
            '-arch', 'x64',
            '-ext', 'WixToolset.UI.wixext',
            '-ext', 'WixToolset.Util.wixext',
            '-d', f'ProductVersion={version}',
            '-d', f'ProjectRoot={ROOT}',
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
    parser.add_argument('--wix', default='wix')
    options = parser.parse_args(argv)
    print(build(
        options.output,
        options.runtime,
        options.dependencies,
        version=options.version,
        wix=options.wix,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
