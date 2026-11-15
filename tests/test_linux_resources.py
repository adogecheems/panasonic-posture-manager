"""Source resource discovery works without an installed desktop payload."""
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests._test_support import require_linux

require_linux()

from panasonic_posture.linux.ui.common import data_root


class ResourceTests(unittest.TestCase):
    def test_source_checkout_resolves_linux_artwork(self):
        with patch.dict(os.environ, {}, clear=True):
            root = data_root()
        self.assertEqual(root, Path(__file__).resolve().parents[1] / 'data/assets/linux')
        for pose in ('laptop', 'tent', 'stand', 'tablet'):
            with self.subTest(pose=pose):
                self.assertTrue((root / f'{pose}.svg').is_file())
        for name in ('lets-note-window-icon.png', 'lets-note-window-icon-dark.png'):
            with self.subTest(name=name):
                self.assertTrue((root / name).is_file())

    def test_explicit_installed_ui_override_is_preserved(self):
        root = Path(__file__).resolve().parent / 'fixture'
        with patch.dict(os.environ, {'PANASONIC_POSTURE_UI_DIR': str(root / 'ui')}):
            self.assertEqual(data_root(), root / 'assets')
