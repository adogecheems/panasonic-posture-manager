"""Localize the per-machine Start menu entry during MSI install and removal."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat

from ..fluent.resources import translator

SOURCE_FOLDER = 'Panasonic Posture Manager'
SOURCE_SHORTCUT = 'Panasonic Posture Manager.lnk'
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
    return translator(language)('Panasonic Posture Manager')


def _programs_root(program_data: Path) -> Path:
    root = Path(program_data).absolute() / START_MENU_RELATIVE_PATH
    if not _ordinary_directory(root):
        raise FileNotFoundError(f'Start menu Programs directory is unavailable: {root}')
    return root


def localize_start_menu(program_data: Path, language_id: int) -> Path:
    """Rename the MSI-created folder and shortcut to the current UI language."""
    display_name = _display_name(language_id)
    root = _programs_root(program_data)
    alternate_name = (
        SOURCE_FOLDER if display_name != SOURCE_FOLDER else _display_name(0x0804)
    )
    desired_folder = root / display_name
    alternate_folder = root / alternate_name
    if not _ordinary_directory(desired_folder) and _ordinary_directory(alternate_folder):
        alternate_folder.rename(desired_folder)
    if not _ordinary_directory(desired_folder):
        raise FileNotFoundError(
            f'application Start menu folder is unavailable: {desired_folder}'
        )

    desired_shortcut = desired_folder / f'{display_name}.lnk'
    alternate_shortcut = desired_folder / f'{alternate_name}.lnk'
    if _ordinary_directory(alternate_folder):
        stale_shortcut = alternate_folder / f'{alternate_name}.lnk'
        if (
            not _ordinary_file(desired_shortcut)
            and not _ordinary_file(alternate_shortcut)
            and _ordinary_file(stale_shortcut)
        ):
            stale_shortcut.rename(desired_shortcut)
        elif _ordinary_file(stale_shortcut):
            stale_shortcut.unlink()
        try:
            alternate_folder.rmdir()
        except OSError:
            pass
    if not _ordinary_file(desired_shortcut) and _ordinary_file(alternate_shortcut):
        alternate_shortcut.rename(desired_shortcut)
    if _ordinary_file(desired_shortcut) and _ordinary_file(alternate_shortcut):
        alternate_shortcut.unlink()
    if not _ordinary_file(desired_shortcut):
        raise FileNotFoundError(
            f'application Start menu shortcut is unavailable: {desired_shortcut}'
        )
    return desired_shortcut


def remove_start_menu_entries(program_data: Path) -> None:
    """Remove only the known localized/base links and empty application folders."""
    root = _programs_root(program_data)
    names = {
        _display_name(0x0409),
        _display_name(0x0804),
    }
    folders = {root / SOURCE_FOLDER}
    folders.update(root / name for name in names)
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
