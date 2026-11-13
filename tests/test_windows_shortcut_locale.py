"""Windows Start menu localization without finding or editing the real shell."""
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.windows.shortcut_locale import (
    LEGACY_FOLDER,
    LEGACY_SHORTCUT,
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
        self.programs.mkdir(parents=True)
        self.shortcut = self.programs / SOURCE_SHORTCUT
        self.shortcut.write_bytes(b'shortcut')

    def test_chinese_ui_renames_root_shortcut_idempotently(self):
        expected = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
        self.assertEqual(localize_start_menu(self.program_data, 0x0804), expected)
        self.assertTrue(expected.is_file())
        self.assertFalse(self.shortcut.exists())
        self.assertFalse((self.programs / SOURCE_FOLDER).exists())
        self.assertEqual(localize_start_menu(self.program_data, 0x1004), expected)
        self.assertEqual(expected.read_bytes(), b'shortcut')

    def test_english_ui_restores_the_base_names(self):
        chinese = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
        self.shortcut.rename(chinese)
        self.assertEqual(localize_start_menu(self.program_data, 0x0409), self.shortcut)
        self.assertTrue(self.shortcut.is_file())
        self.assertFalse(chinese.exists())
        self.assertFalse((self.programs / SOURCE_FOLDER).exists())

    def test_traditional_chinese_uses_english_installer_names(self):
        chinese = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
        for language_id in (0x0404, 0x0C04, 0x1404):
            with self.subTest(language_id=language_id):
                self.shortcut.rename(chinese)
                self.assertEqual(
                    localize_start_menu(self.program_data, language_id),
                    self.shortcut,
                )
                self.assertTrue(self.shortcut.is_file())
                self.assertFalse(chinese.exists())

    def test_removal_only_deletes_known_root_links_and_empty_folders(self):
        localized_root = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
        localized_root.write_bytes(b'shortcut')
        legacy_root = self.programs / LEGACY_SHORTCUT
        legacy_root.write_bytes(b'legacy')
        unrelated_root = self.programs / 'user-link.lnk'
        unrelated_root.write_bytes(b'keep')

        localized = self.programs / '松下二合一平板电脑姿态管理工具'
        localized.mkdir()
        (localized / '松下二合一平板电脑姿态管理工具.lnk').write_bytes(b'shortcut')
        retained = localized / 'user-note.txt'
        retained.write_text('keep', encoding='utf-8')
        base_folder = self.programs / SOURCE_FOLDER
        base_folder.mkdir()
        (base_folder / SOURCE_SHORTCUT).write_bytes(b'shortcut')

        remove_start_menu_entries(self.program_data)
        self.assertFalse(self.shortcut.exists())
        self.assertFalse(localized_root.exists())
        self.assertFalse(legacy_root.exists())
        self.assertTrue(unrelated_root.exists())
        self.assertFalse(base_folder.exists())
        self.assertTrue(localized.is_dir())
        self.assertEqual([path.name for path in localized.iterdir()], ['user-note.txt'])

    def test_legacy_folder_shortcut_is_migrated_to_the_programs_root(self):
        self.shortcut.unlink()
        legacy = self.programs / LEGACY_FOLDER
        legacy.mkdir()
        (legacy / LEGACY_SHORTCUT).write_bytes(b'legacy')
        expected = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
        self.assertEqual(localize_start_menu(self.program_data, 0x0804), expected)
        self.assertEqual(expected.read_bytes(), b'legacy')
        self.assertFalse(legacy.exists())

    def test_old_folder_is_removed_only_after_migrating_its_known_link(self):
        self.shortcut.unlink()
        old_folder = self.programs / SOURCE_FOLDER
        old_folder.mkdir()
        (old_folder / SOURCE_SHORTCUT).write_bytes(b'shortcut')
        retained = old_folder / 'user-note.txt'
        retained.write_text('keep', encoding='utf-8')

        expected = self.programs / '松下二合一平板电脑姿态管理工具.lnk'
        self.assertEqual(localize_start_menu(self.program_data, 0x0804), expected)
        self.assertEqual(expected.read_bytes(), b'shortcut')
        self.assertTrue(old_folder.is_dir())
        self.assertEqual([path.name for path in old_folder.iterdir()], ['user-note.txt'])

    def test_missing_installer_shortcut_and_invalid_language_are_rejected(self):
        self.shortcut.unlink()
        with self.assertRaises(FileNotFoundError):
            localize_start_menu(self.program_data, 0x0804)
        for value in (-1, 0x10000, True, '2052'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                localize_start_menu(self.program_data, value)


if __name__ == '__main__':
    unittest.main()
