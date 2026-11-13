from pathlib import Path
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


class InstallerProgressLayoutTests(unittest.TestCase):
    def test_status_text_has_room_above_the_progress_bar(self):
        for culture in ('en-US', 'zh-CN'):
            with self.subTest(culture=culture):
                root = ET.parse(
                    ROOT
                    / 'packaging/windows/msi'
                    / f'Setup.{culture}.wxl'
                ).getroot()
                overrides = {
                    (element.get('Dialog'), element.get('Control')): element
                    for element in root
                    if element.tag.endswith('UI')
                }

                for control_id in ('StatusLabel', 'ActionText'):
                    control = overrides[('ProgressDlg', control_id)]
                    self.assertEqual(control.get('Y'), '100')
                    self.assertEqual(
                        control.get('Height'),
                        '15',
                        'The 9pt CJK line needs more than WiX default 10-DLU height',
                    )

                progress_bar = overrides[('ProgressDlg', 'ProgressBar')]
                self.assertEqual(progress_bar.get('Y'), '118')
                self.assertEqual(progress_bar.get('Height'), '10')


if __name__ == '__main__':
    unittest.main()
