"""Localize the per-machine Start menu entry during MSI install and removal."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat

from ..fluent.resources import application_name
from ..product import APPLICATION_DISPLAY_NAME

SOURCE_FOLDER = APPLICATION_DISPLAY_NAME
SOURCE_SHORTCUT = f'{APPLICATION_DISPLAY_NAME}.lnk'
LEGACY_FOLDER = 'Panasonic Posture Manager'
LEGACY_SHORTCUT = f'{LEGACY_FOLDER}.lnk'
START_MENU_RELATIVE_PATH = Path('Microsoft/Windows/Start Menu/Programs')


def _ordinary_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode) and not path.is_symlink()
    except FileNotFoundError:
        return False


def _ordinary_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode) and not path.is_symlink()
    except FileNotFoundError:
        return False


def _display_name(language_id: int) -> str:
    if type(language_id) is not int or not 0 <= language_id <= 0xFFFF:
        raise ValueError('Windows language ID must be an unsigned 16-bit integer')
    language = 'zh_CN' if language_id & 0x3FF == 0x04 else 'en'
    return application_name(language)


def _known_names() -> tuple[str, ...]:
    names = (
        application_name('en'),
        application_name('zh_CN'),
        LEGACY_FOLDER,
    )
    return tuple(dict.fromkeys(names))


def _programs_root(program_data: Path) -> Path:
    root = Path(program_data).absolute() / START_MENU_RELATIVE_PATH
    if not _ordinary_directory(root):
        raise FileNotFoundError(f'Start menu Programs directory is unavailable: {root}')
    return root


def localize_start_menu(program_data: Path, language_id: int) -> Path:
    """Rename the MSI-created folder and shortcut to the current UI language."""
    display_name = _display_name(language_id)
    root = _programs_root(program_data)
    desired_folder = root / display_name
    if not _ordinary_directory(desired_folder):
        for name in _known_names():
            candidate = root / name
            if _ordinary_directory(candidate):
                candidate.rename(desired_folder)
                break
    if not _ordinary_directory(desired_folder):
        raise FileNotFoundError(
            f'application Start menu folder is unavailable: {desired_folder}'
        )

    desired_shortcut = desired_folder / f'{display_name}.lnk'
    if not _ordinary_file(desired_shortcut):
        folders = [desired_folder]
        folders.extend(root / name for name in _known_names() if name != display_name)
        for folder in folders:
            for name in _known_names():
                candidate = folder / f'{name}.lnk'
                if _ordinary_file(candidate):
                    candidate.rename(desired_shortcut)
                    break
            if _ordinary_file(desired_shortcut):
                break
    for name in _known_names():
        duplicate = desired_folder / f'{name}.lnk'
        if duplicate != desired_shortcut and _ordinary_file(duplicate):
            duplicate.unlink()
    if not _ordinary_file(desired_shortcut):
        raise FileNotFoundError(
            f'application Start menu shortcut is unavailable: {desired_shortcut}'
        )

    for name in _known_names():
        stale_folder = root / name
        if stale_folder == desired_folder or not _ordinary_directory(stale_folder):
            continue
        for shortcut_name in _known_names():
            stale_shortcut = stale_folder / f'{shortcut_name}.lnk'
            if _ordinary_file(stale_shortcut):
                stale_shortcut.unlink()
        try:
            stale_folder.rmdir()
        except OSError:
            # Preserve any user-created files rather than deleting the folder.
            pass
    return desired_shortcut


def remove_start_menu_entries(program_data: Path) -> None:
    """Remove only the known localized/base links and empty application folders."""
    root = _programs_root(program_data)
    names = _known_names()
    folders = {root / name for name in names}
    for folder in folders:
        if not _ordinary_directory(folder):
            continue
        for name in {SOURCE_SHORTCUT}.union(f'{value}.lnk' for value in names):
            shortcut = folder / name
            if _ordinary_file(shortcut):
                shortcut.unlink()
        try:
            folder.rmdir()
        except OSError:
            # Preserve any user-created files rather than deleting the folder.
            pass


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--install', action='store_true')
    action.add_argument('--remove', action='store_true')
    parser.add_argument('--language-id', required=True, type=lambda value: int(value, 0))
    options = parser.parse_args(argv)
    program_data = Path(os.environ.get('ProgramData', r'C:\ProgramData'))
    if options.install:
        localize_start_menu(program_data, options.language_id)
    else:
        remove_start_menu_entries(program_data)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
