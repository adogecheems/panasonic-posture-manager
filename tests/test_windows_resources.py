"""Windows application resources, independent of the widget toolkit."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


class WindowsResourceTests(unittest.TestCase):
    def test_shared_and_native_assets_resolve_without_cross_platform_copies(self):
        from panasonic_posture.windows.app.resources import asset_path
        with patch.dict(os.environ, PANASONIC_POSTURE_DATA_DIR=str(ROOT / 'data')):
            for name in ('postures/laptop.svg', 'branding/lets-note-window-icon.png',
                         'icons/work.mmoe.panasonic.PostureManager.png'):
                with self.subTest(name=name):
                    self.assertEqual(asset_path(name), ROOT / 'data/assets/common' / name)
            name = 'icons/work.mmoe.panasonic.PostureManager-square.png'
            self.assertEqual(asset_path(name), ROOT / 'data/assets/windows' / name)
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / 'data'
            name = 'postures/laptop.svg'
            path = data / 'assets' / name
            path.parent.mkdir(parents=True)
            path.write_text('installed asset', encoding='utf-8')
            with patch.dict(os.environ, PANASONIC_POSTURE_DATA_DIR=str(data)):
                self.assertEqual(asset_path(name), path)

    def test_assets_resolve_in_source_and_installed_layouts(self):
        from panasonic_posture.windows.app.resources import asset_root
        for layout in ('source', 'installed'):
            with self.subTest(layout=layout), tempfile.TemporaryDirectory() as directory:
                data = Path(directory) / 'data'
                assets = data / 'assets'
                if layout == 'source':
                    assets /= 'windows'
                assets.mkdir(parents=True)
                with patch.dict(os.environ, PANASONIC_POSTURE_DATA_DIR=str(data)):
                    self.assertEqual(asset_root(), assets)

    @unittest.skipUnless(sys.platform == 'win32', 'branding uses Windows image codecs')
    def test_dark_wordmark_preserves_the_source_transparency(self):
        powershell = shutil.which('powershell.exe')
        if powershell is None:
            self.skipTest('Windows PowerShell is unavailable')
        # The former dark asset made near-transparent edge noise opaque.
        # Compare decoded alpha rather than PNG bytes or encoder metadata.
        command = r'''
Add-Type -AssemblyName System.Drawing
$root = Join-Path (Get-Location) 'data/assets/common/branding'
$light = [System.Drawing.Bitmap]::new((Join-Path $root 'lets-note-window-icon.png'))
$dark = [System.Drawing.Bitmap]::new((Join-Path $root 'lets-note-window-icon-dark.png'))
try {
    if ($light.Size -ne $dark.Size) { throw 'Wordmark dimensions differ' }
    for ($y = 0; $y -lt $light.Height; $y++) {
        for ($x = 0; $x -lt $light.Width; $x++) {
            if ($light.GetPixel($x, $y).A -ne $dark.GetPixel($x, $y).A) {
                throw "Wordmark alpha differs at $x,$y"
            }
        }
    }
} finally {
    $light.Dispose()
    $dark.Dispose()
}
'''
        result = subprocess.run(
            [powershell, '-NoProfile', '-NonInteractive', '-Command',
             "$ErrorActionPreference = 'Stop'; " + command],
            cwd=ROOT, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
