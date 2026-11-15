"""Regression tests for the repository-wide i18n gate."""

from collections import Counter
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from tests import check_i18n


class I18nCheckTests(unittest.TestCase):
    def test_installer_launch_label_is_required_in_both_cultures(self):
        for culture in ('en-US', 'zh-CN'):
            with self.subTest(culture=culture), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                target = root / check_i18n.MSI_LOCALIZATION_DIR
                target.mkdir(parents=True)
                for source in (check_i18n.REPO_ROOT / check_i18n.MSI_LOCALIZATION_DIR).glob('Setup.*.wxl'):
                    tree = ET.parse(source)
                    if tree.getroot().get('Culture') == culture:
                        for element in list(tree.getroot()):
                            if element.get('Id') == 'LaunchFrontendText':
                                tree.getroot().remove(element)
                    tree.write(target / source.name, encoding='utf-8')
                errors = []
                with patch.object(check_i18n, 'REPO_ROOT', root):
                    check_i18n._check_installer_metadata(errors)
                self.assertTrue(any(
                    f'MSI localization {culture} misses strings' in error
                    and 'LaunchFrontendText' in error for error in errors
                ), errors)

    def test_source_scan_uses_python_syntax_and_handles_adjacent_literals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.py'
            path.write_text(
                "\"\"\"_('not a source string')\"\"\"\n"
                "# _('not a comment')\n"
                "label = _('First ' 'line.')\n",
                encoding='utf-8',
            )
            self.assertEqual(
                check_i18n.extracted_msgids(path),
                ['First line.'],
            )

    def test_placeholder_parity_checks_printf_and_braces(self):
        self.assertEqual(
            check_i18n.placeholders('Hello %(name)s {version}'),
            (Counter({'%(name)s': 1}), Counter({'version': 1})),
        )
        source = check_i18n.placeholders('New version available: {version}')
        translated = check_i18n.placeholders('发现新版本：')
        self.assertNotEqual(source, translated)

    def test_repository_metadata_and_catalog_pass(self):
        self.assertEqual(check_i18n.run(), 0)


if __name__ == '__main__':
    unittest.main()
