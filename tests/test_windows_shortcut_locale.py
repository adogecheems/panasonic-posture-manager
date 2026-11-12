"""Windows Start menu localization without finding or editing the real shell."""
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.windows.shortcut_locale import (
    SOURCE_FOLDER,
    SOURCE_SHORTCUT,
    START_MENU_RELATIVE_PATH,
    localize_start_menu,
    remove_start_menu_entries,
)


class ShortcutLocaleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.program_data = Path(self.temporary.name)
        self.programs = self.program_data / START_MENU_RELATIVE_PATH
        self.folder = self.programs / SOURCE_FOLDER
        self.folder.mkdir(parents=True)
        self.shortcut = self.folder / SOURCE_SHORTCUT
        self.shortcut.write_bytes(b'shortcut')

    def test_chinese_ui_renames_folder_and_shortcut_idempotently(self):
        chinese = self.programs / '松下姿态管理器'
        expected = chinese / '松下姿态管理器.lnk'
        self.assertEqual(localize_start_menu(self.program_data, 0x0804), expected)
        self.assertTrue(expected.is_file())
        self.assertFalse(self.folder.exists())
        self.assertEqual(localize_start_menu(self.program_data, 0x0404), expected)
        self.assertEqual(expected.read_bytes(), b'shortcut')

    def test_english_ui_restores_the_base_names(self):
        chinese = self.programs / '松下姿态管理器'
        chinese.mkdir()
        expected = chinese / '松下姿态管理器.lnk'
        expected.write_bytes(b'shortcut')
        self.assertEqual(localize_start_menu(self.program_data, 0x0409), self.shortcut)
        self.assertTrue(self.shortcut.is_file())
        self.assertFalse(chinese.exists())

    def test_removal_only_deletes_known_links_and_empty_folders(self):
        localized = self.programs / '松下姿态管理器'
        localized.mkdir()
        (localized / '松下姿态管理器.lnk').write_bytes(b'shortcut')
        retained = localized / 'user-note.txt'
        retained.write_text('keep', encoding='utf-8')
        remove_start_menu_entries(self.program_data)
        self.assertFalse(self.folder.exists())
        self.assertTrue(localized.is_dir())
        self.assertEqual([path.name for path in localized.iterdir()], ['user-note.txt'])

    def test_missing_installer_shortcut_and_invalid_language_are_rejected(self):
        self.shortcut.unlink()
        with self.assertRaises(FileNotFoundError):
            localize_start_menu(self.program_data, 0x0804)
        for value in (-1, 0x10000, True, '2052'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                localize_start_menu(self.program_data, value)


if __name__ == '__main__':
    unittest.main()
