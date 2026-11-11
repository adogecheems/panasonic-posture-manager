"""Exercise an extracted Windows preview with only its bundled interpreter."""
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
import sys, time
from pathlib import Path
from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFontDatabase
from PyQt6.QtTest import QTest
from panasonic_posture.fluent.app import Window
from panasonic_posture.windows.client import ProcessClient
app = QApplication([])
assert QFontDatabase.families(), 'Bundle verification requires installed system fonts'
client = ProcessClient(Path(sys.argv[1]), simulate=True)
window = Window(backend=True, client=client, language='zh_CN', settings_path=Path(sys.argv[1]))
window.show()
deadline = time.monotonic() + 10
while window.settings.model.state is None and time.monotonic() < deadline:
    QTest.qWait(50)
assert window.settings.model.state is not None
window.settings.radios['tent'].click()
deadline = time.monotonic() + 10
while (window.settings.model.state['applied'] or {}).get('mode') != 'tent' and time.monotonic() < deadline:
    QTest.qWait(50)
assert window.settings.model.state['applied']['mode'] == 'tent'
assert window.home.heading.text() == '帐篷'
process = window.client.process
window.close()
assert process.poll() is not None
'''
        env = dict(os.environ, QT_QPA_PLATFORM='offscreen')
        if os.name == 'nt':
            env.setdefault('QT_QPA_FONTDIR', str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts'))
        env.pop('PYTHONPATH', None)
        for executable in ('python.exe', 'pythonw.exe'):
            result = subprocess.run([str(root / 'runtime' / executable), '-I', '-c', code,
                                     str(Path(directory) / 'settings.json')],
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
    print('Extracted Windows runtime, Qt resources and backend IPC passed')


if __name__ == '__main__':
    main()
