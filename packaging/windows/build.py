"""Build a self-contained Windows portable ZIP using an existing runtime.

Run on Windows after installing requirements-ui.txt into a separate --target
directory. This script never downloads packages, installs a service, modifies
the host Python or publishes a release.
"""
import argparse
import fnmatch
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
PYQT6_LICENSE = ROOT / 'packaging/windows/PyQt6-LICENSE'


def _application_display_name() -> str:
    source = ROOT / 'src/panasonic_posture/product.py'
    spec = importlib.util.spec_from_file_location('posture_product', source)
    if spec is None or spec.loader is None:
        raise RuntimeError('cannot load the application display name')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.APPLICATION_DISPLAY_NAME)


APPLICATION_DISPLAY_NAME = _application_display_name()


def application_files(runtime: Path, dependencies: Path) -> dict[str, Path | bytes]:
    for name in ('python.exe', 'pythonw.exe', 'LICENSE.txt', 'Lib/os.py'):
        if not (runtime / name).is_file():
            raise ValueError(f'Windows Python runtime is missing {name}')
    for name in ('PyQt6', 'qfluentwidgets', 'qframelesswindow'):
        if not (dependencies / name).is_dir():
            raise ValueError(f'UI dependency is missing {name}')
    metadata = list(dependencies.glob('*.dist-info'))
    has_license = lambda directory: any(
        'license' in path.name.lower() or 'copying' in path.name.lower()
        for path in directory.rglob('*') if path.is_file()
    )
    missing_licenses = [directory for directory in metadata if not has_license(directory)]
    if (not metadata
            or any(not directory.name.casefold().startswith('pyqt6-')
                   for directory in missing_licenses)
            or not PYQT6_LICENSE.is_file()):
        raise ValueError('dependency license metadata is missing')
    files: dict[str, Path | bytes] = {}

    def add_tree(source: Path, prefix: str, ignored=()):
        def excluded(name):
            return any(fnmatch.fnmatch(name, pattern) for pattern in ignored)
        for directory, children, names in os.walk(source, followlinks=True):
            children[:] = sorted(name for name in children if not excluded(name))
            for name in sorted(names):
                if not excluded(name):
                    path = Path(directory) / name
                    files[prefix + '/' + path.relative_to(source).as_posix()] = path

    development_trees = ('__pycache__', '*.pyc', 'test', 'tests', 'test_*.py', '*_test.py',
                         'demo', 'demos', 'example', 'examples', 'samples', 'testdata',
                         'idlelib', 'turtledemo', 'turtle.py', 'unittest', 'doctest.py',
                         'bindings', 'scripts')
    add_tree(runtime, 'runtime', ('site-packages', 'Scripts',
                                 'Doc', 'Tools', 'include', 'libs', 'tcl',
                                 *development_trees,
                                 'python*._pth'))
    add_tree(dependencies, 'runtime/Lib/site-packages',
             ('*.chm', 'qml', 'pywin32_test*', *development_trees))
    # PyQt6 6.8 wheels omit the license from their dist-info metadata. Carry
    # the exact GPLv3 text from the matching upstream source distribution.
    for directory in missing_licenses:
        files[f'runtime/Lib/site-packages/{directory.name}/LICENSE'] = PYQT6_LICENSE
    add_tree(ROOT / 'src/panasonic_posture', 'app/panasonic_posture', ('__pycache__', '*.pyc'))
    add_tree(ROOT / 'data/assets', 'data/assets')
    add_tree(ROOT / 'data/profiles', 'data/profiles')
    files['po/zh_CN.po'] = ROOT / 'po/zh_CN.po'
    for name in ('LICENSE', 'README.md', 'MAINTENANCE.md'):
        files[name] = ROOT / name
    # UI interpreters need bundled Qt; the separately named hardware
    # interpreter must never import site packages or process site hooks.
    ui_search_paths = 'Lib\nDLLs\nLib/site-packages\n../app\nimport site\n'
    hardware_search_paths = 'Lib\nDLLs\n../app\n'
    files['runtime/python-hardware.exe'] = runtime / 'python.exe'
    files['runtime/python-hardware._pth'] = hardware_search_paths.encode('utf-8')
    for executable in ('python', 'pythonw'):
        files[f'runtime/{executable}._pth'] = ui_search_paths.encode('utf-8')
    files['launch.py'] = (
        b'import sys\n'
        b'from pathlib import Path\n'
        b'sys.path.insert(0, str(Path(__file__).resolve().parent / "app"))\n'
        b'from panasonic_posture.fluent.__main__ import main\n'
        b'raise SystemExit(main())\n'
    )
    for name, arguments in ((f'{APPLICATION_DISPLAY_NAME}.cmd', ''),
                             ('Hardware.cmd', '')):
        files[name] = (
            '@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
            f'"%~dp0runtime\\pythonw.exe" -I "%~dp0launch.py" {arguments} %*\r\n').encode('utf-8')
    return files


def file_digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def stage_application(destination: Path, runtime: Path, dependencies: Path) -> None:
    """Materialize the same file set for fixtures or explicit tree inspection."""
    if destination.exists():
        raise ValueError('destination already exists')
    files = application_files(runtime, dependencies)
    destination.mkdir(parents=True)
    manifest = {}
    for name, value in sorted(files.items()):
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            target.write_bytes(value)
        else:
            shutil.copy2(value, target)
        manifest[name] = file_digest(target)
    (destination / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8', newline='\n')


def build(output: Path, runtime: Path, dependencies: Path) -> Path:
    files = application_files(runtime, dependencies)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'panasonic-posture-windows-portable.zip'
    # Stream each original file once into the archive while hashing its exact
    # bytes. Avoid thousands of redundant Windows file writes and an in-memory
    # copy of a complete Qt DLL or ZIP. Keep an existing artifact on failure.
    with tempfile.TemporaryDirectory(prefix='posture-windows-', dir=output) as temporary:
        temporary_zip = Path(temporary) / archive.name
        manifest = {}
        with zipfile.ZipFile(temporary_zip, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for name, value in sorted(files.items()):
                archive_name = 'PanasonicPostureManager/' + name
                digest = hashlib.sha256()
                if isinstance(value, bytes):
                    bundle.writestr(archive_name, value)
                    digest.update(value)
                else:
                    info = zipfile.ZipInfo.from_file(value, archive_name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with value.open('rb') as source, bundle.open(info, 'w') as target:
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk)
                            target.write(chunk)
                manifest[name] = digest.hexdigest()
            bundle.writestr('PanasonicPostureManager/MANIFEST.json',
                            json.dumps(manifest, indent=2) + '\n')
        checksum = file_digest(temporary_zip)
        temporary_zip.replace(archive)
    archive.with_suffix('.zip.sha256').write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    return archive


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, default=Path(sys.base_prefix))
    parser.add_argument('--dependencies', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/windows')
    options = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('build the Windows runtime bundle on Windows')
    print(build(options.output, options.runtime, options.dependencies))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
