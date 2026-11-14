"""Language selection for the shipped English and Simplified Chinese catalogs."""

import unittest

from panasonic_posture.localization import (
    catalog_language,
    catalog_languages,
    environment_languages,
)
from panasonic_posture.windows.localization import windows_catalog_language


class LocalizationTests(unittest.TestCase):
    def test_simplified_chinese_tags_use_the_shipped_catalog(self):
        for tag in ("zh", "zh_CN.UTF-8", "zh-Hans", "zh-Hans-CN", "zh-SG"):
            with self.subTest(tag=tag):
                self.assertEqual(catalog_language(tag), "zh_CN")
        self.assertEqual(catalog_language("fr_FR.UTF-8"), "fr_FR")

    def test_traditional_and_unknown_chinese_tags_fall_back_to_english(self):
        for tag in ("zh_TW.UTF-8", "zh-HK", "zh-Hant", "zh-Hant-CN", "zh-MO"):
            with self.subTest(tag=tag):
                self.assertIsNone(catalog_language(tag))
        self.assertEqual(catalog_languages(("zh_TW", "en")), ("en",))
        self.assertEqual(
            catalog_languages(("zh_TW", "zh_CN", "en")), ("en",)
        )
        self.assertEqual(
            catalog_languages(("zh_CN", "zh_TW", "en")), ("zh_CN", "en")
        )

    def test_windows_language_ids_are_not_reduced_to_the_primary_language(self):
        for language_id in (0x0004, 0x0804, 0x1004):
            with self.subTest(language_id=language_id):
                self.assertEqual(
                    windows_catalog_language(language_id), "zh_CN"
                )
        for language_id in (0x0404, 0x0C04, 0x1404, 0x0409):
            with self.subTest(language_id=language_id):
                self.assertEqual(windows_catalog_language(language_id), "en")

    def test_environment_language_list_keeps_gettext_precedence(self):
        environment = {
            "LANGUAGE": "zh_TW:zh_CN:en",
            "LC_ALL": "fr_FR.UTF-8",
            "LC_MESSAGES": "de_DE.UTF-8",
            "LANG": "en_US.UTF-8",
        }
        self.assertEqual(
            environment_languages(environment),
            ("zh_TW", "zh_CN", "en"),
        )
        self.assertEqual(
            catalog_languages(environment_languages(environment)),
            ("en",),
        )


if __name__ == "__main__":
    unittest.main()
