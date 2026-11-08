"""Build a self-contained Windows software-preview ZIP using an existing runtime.

Run on Windows after installing requirements-ui.txt into a separate --target
directory. This script never downloads packages, installs a service, modifies
the host Python or publishes a release.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]


def stage_application(destination: Path, runtime: Path, dependencies: Path) -> None:
    if destination.exists():
        raise ValueError('destination already exists')
    for name in ('python.exe', 'pythonw.exe', 'LICENSE.txt', 'Lib/os.py'):
        if not (runtime / name).is_file():
            raise ValueError(f'Windows Python runtime is missing {name}')
    for name in ('PyQt6', 'qfluentwidgets', 'qframelesswindow'):
        if not (dependencies / name).is_dir():
            raise ValueError(f'preview dependency is missing {name}')
    metadata = list(dependencies.glob('*.dist-info'))
    if not metadata or any(not any('license' in path.name.lower() or 'copying' in path.name.lower()
                                  for path in directory.rglob('*') if path.is_file())
                           for directory in metadata):
        raise ValueError('dependency license metadata is missing')
    destination.mkdir(parents=True)
    ignored = shutil.ignore_patterns('__pycache__', '*.pyc', 'site-packages', 'Scripts',
                                      'Doc', 'Tools', 'include', 'libs', 'tcl', 'test', 'tests')
    shutil.copytree(runtime, destination / 'runtime', ignore=ignored)
    shutil.copytree(dependencies, destination / 'runtime/Lib/site-packages',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(ROOT / 'src/panasonic_posture', destination / 'app/panasonic_posture',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(ROOT / 'data/assets', destination / 'data/assets')
    (destination / 'po').mkdir()
    shutil.copy2(ROOT / 'po/zh_CN.po', destination / 'po/zh_CN.po')
    for name in ('LICENSE', 'README.md', 'MAINTENANCE.md'):
        shutil.copy2(ROOT / name, destination / name)
    # Isolate both parent and backend from registry/user-site/PYTHONPATH.
    search_paths = 'Lib\nDLLs\nLib/site-packages\n../app\nimport site\n'
    for executable in ('python', 'pythonw'):
        (destination / f'runtime/{executable}._pth').write_text(search_paths, encoding='utf-8')
    # The DLL-specific file takes priority over python._pth when present.
    for library in (destination / 'runtime').glob('python3[0-9]*.dll'):
        library.with_suffix('._pth').write_text(search_paths, encoding='utf-8')
    for existing in (destination / 'runtime').glob('python[0-9]*._pth'):
        existing.write_text(search_paths, encoding='utf-8')
    (destination / 'launch.py').write_text(
        'from panasonic_posture.fluent.__main__ import main\nraise SystemExit(main())\n', encoding='utf-8')
    for name, arguments in (('Panasonic Posture Manager.cmd', ''),
                             ('Simulation.cmd', '--simulate-backend')):
        (destination / name).write_text(
            '@echo off\r\nsetlocal\r\ncd /d "%~dp0"\r\n'
            f'"%~dp0runtime\\pythonw.exe" -I "%~dp0launch.py" {arguments} %*\r\n',
            encoding='utf-8', newline='')
    manifest = {path.relative_to(destination).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(destination.rglob('*')) if path.is_file()}
    (destination / 'MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')


def build(output: Path, runtime: Path, dependencies: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'panasonic-posture-windows-preview.zip'
    with tempfile.TemporaryDirectory(prefix='posture-windows-') as temporary:
        staged = Path(temporary) / 'PanasonicPostureManager'
        stage_application(staged, runtime, dependencies)
        temporary_zip = Path(temporary) / archive.name
        with zipfile.ZipFile(temporary_zip, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in sorted(staged.rglob('*')):
                if path.is_file():
                    bundle.write(path, path.relative_to(staged.parent))
        shutil.copyfile(temporary_zip, archive)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
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
