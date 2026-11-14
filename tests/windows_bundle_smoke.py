"""Exercise an extracted Windows portable app with only its bundled interpreter."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


def main():
    root = Path(os.environ['POSTURE_BUNDLE'])
    manifest = json.loads((root / 'MANIFEST.json').read_text(encoding='utf-8'))
    for name, digest in manifest.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest, name
    with tempfile.TemporaryDirectory() as directory:
        code = '''
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtTest import QTest
from panasonic_posture.windows.ui.app import Window
from panasonic_posture.windows.ui.unavailable import UnavailableClient
app = QApplication([])
assert QFontDatabase.families(), 'Bundle verification requires installed system fonts'
window = Window(client=UnavailableClient(), language='zh_CN')
window.show()
QTest.qWait(100)
assert '未连接' in window.home.detail.text()
assert window.windowTitle() == '松下二合一平板电脑姿态管理工具'
window.close()
'''
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        if os.name == 'nt':
            env.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts'))
        env.pop('PYTHONPATH', None)
        for executable in ('python.exe', 'pythonw.exe'):
            result = subprocess.run([str(root / 'runtime' / executable), '-I', '-c', code],
                                    env=env, cwd=directory, text=True, capture_output=True, timeout=40)
            if result.returncode:
                raise RuntimeError(executable + ': ' + result.stdout + result.stderr)
        restricted = '''
import sys
from panasonic_posture.windows.hardware_application import create_hardware_runtime
assert create_hardware_runtime
try:
    import PyQt6
except ModuleNotFoundError:
    pass
else:
    raise AssertionError('hardware interpreter imported UI site packages')
'''
        result = subprocess.run(
            [str(root / 'runtime/python-hardware.exe'), '-B', '-I', '-c', restricted],
            env={key: value for key, value in env.items() if key != 'PYTHONPATH'},
            cwd=directory,
            text=True,
            capture_output=True,
            timeout=40,
        )
        if result.returncode:
            raise RuntimeError('python-hardware.exe: ' + result.stdout + result.stderr)
    print('Extracted Windows runtime, Qt resources and hardware module boundary passed')


if __name__ == '__main__':
    main()
