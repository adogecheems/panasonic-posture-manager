"""Font setup must also cover the lightweight tray before Fluent loads."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import unittest


@unittest.skipUnless(importlib.util.find_spec('PyQt6'), 'optional Qt is not installed')
class WindowsFontTests(unittest.TestCase):
    def test_lightweight_startup_configures_text_and_safe_font_fallbacks(self):
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        if sys.platform == 'win32':
            env.setdefault('QT_QPA_FONTDIR', str(Path(os.environ['SystemRoot']) / 'Fonts'))
        result = subprocess.run([sys.executable, '-c', '''
import sys
from unittest.mock import patch
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QMenu
from panasonic_posture.windows.ui.launcher import _configure_application

class Block:
    def find_spec(self, name, path=None, target=None):
        if name.startswith(('qfluentwidgets', 'qframelesswindow')):
            raise AssertionError(name)
sys.meta_path.insert(0, Block())
app = QApplication([])
original = QFont(app.font())
for available, fallback in (({'Microsoft YaHei UI'}, 'Microsoft YaHei UI'),
                            ({'Microsoft YaHei'}, 'Microsoft YaHei'),
                            (set(), None)):
    app.setFont(original)
    with patch('PyQt6.QtGui.QFontDatabase.families', return_value=list(available)):
        _configure_application(app, 'zh_CN')
        first = app.font()
        _configure_application(app, 'zh_CN')
        assert app.font() == first, 'Repeated configuration must be idempotent'
    expected = list(original.families())
    if fallback and fallback not in expected:
        expected.append(fallback)
    assert app.font().families() == expected
    menu = QMenu()
    menu.addAction('打开 Open')
    menu.ensurePolished()
    font = menu.font()
    assert font.hintingPreference() == QFont.HintingPreference.PreferVerticalHinting
    assert font.styleStrategy() & QFont.StyleStrategy.PreferAntialias
    assert font.styleStrategy() & QFont.StyleStrategy.PreferOutline
    assert not font.styleStrategy() & (QFont.StyleStrategy.NoAntialias |
                                       QFont.StyleStrategy.NoSubpixelAntialias)
    menu.close()
'''], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
