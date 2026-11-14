"""Windows application resources, independent of the widget toolkit."""
import os
from pathlib import Path
import unittest


class WindowsResourceTests(unittest.TestCase):
    def test_source_catalog_fallback(self):
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from panasonic_posture.windows.app.resources import translator
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'po').mkdir()
            (root / 'po/zh_CN.po').write_text('msgid "Laptop"\nmsgstr "笔记本"\n', encoding='utf-8')
            with patch.dict(os.environ, PANASONIC_POSTURE_DATA_DIR=str(root / 'data')):
                self.assertEqual(translator('zh_CN')('Laptop'), '笔记本')
                self.assertEqual(translator('zh-Hans')('Laptop'), '笔记本')
                self.assertEqual(translator('zh_TW')('Laptop'), 'Laptop')
                self.assertEqual(translator('en')('Laptop'), 'Laptop')

    def test_application_display_name_matches_the_window_title_source(self):
        from panasonic_posture.windows.app.resources import application_name
        from panasonic_posture.product import APPLICATION_DISPLAY_NAME
        self.assertEqual(application_name('en'), APPLICATION_DISPLAY_NAME)
        self.assertEqual(
            application_name('zh_CN'), '松下二合一平板电脑姿态管理工具'
        )

    def test_windows_ui_language_wins_over_process_lang(self):
        from unittest.mock import patch
        from panasonic_posture.windows.app.resources import default_language
        environment = {'LANGUAGE': '', 'LC_ALL': '', 'LC_MESSAGES': '', 'LANG': 'C.UTF-8'}
        with patch.dict(os.environ, environment), \
                patch('panasonic_posture.windows.app.resources._windows_language',
                      return_value='zh_CN'):
            self.assertEqual(default_language(), 'zh_CN')
        with patch.dict(os.environ, environment), \
                patch('panasonic_posture.windows.app.resources._windows_language',
                      return_value=None):
            self.assertEqual(default_language(), 'C.UTF-8')
        with patch.dict(os.environ, dict(environment, LC_ALL='zh_CN.UTF-8')), \
                patch('panasonic_posture.windows.app.resources._windows_language',
                      return_value=None):
            self.assertEqual(default_language(), 'zh_CN.UTF-8')

    def test_new_source_strings_survive_an_older_compiled_catalog(self):
        import gettext
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from panasonic_posture.windows.app.resources import translator
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'po').mkdir()
            (root / 'po/zh_CN.po').write_text('msgid "Stand"\nmsgstr "支架"\n', encoding='utf-8')
            compiled = gettext.GNUTranslations()
            compiled._catalog = {'Laptop': '笔记本'}
            compiled.plural = lambda n: int(n != 1)
            with patch.dict(os.environ, PANASONIC_POSTURE_DATA_DIR=str(root / 'data')):
                with patch('gettext.translation', return_value=compiled):
                    self.assertEqual(translator('zh_CN')('Laptop'), '笔记本')
                    self.assertEqual(translator('zh_CN')('Stand'), '支架')


if __name__ == '__main__':
    unittest.main()
