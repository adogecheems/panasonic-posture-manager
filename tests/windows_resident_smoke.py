"""Explicit native tray lifetime exercise; does not register login startup."""
import argparse
import os
import json
from pathlib import Path
import sys
import subprocess
import time

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from panasonic_posture.fluent.app import Window
from panasonic_posture.fluent.resident import ResidentTray
from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.activation import WindowActivation
from panasonic_posture.windows.client import settings_path
from panasonic_posture.windows.display import NativeDisplay
from panasonic_posture.windows.screen_session import require_native_user
from panasonic_posture.windows.startup import LoginStartup, startup_command, NativeRunRegistry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-resident', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    options = parser.parse_args()
    if sys.platform != 'win32': parser.error('requires an authorized Windows machine')
    require_native_user()
    root = options.directory.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if (root / 'result.json').exists(): raise RuntimeError('use a new evidence directory')
    journal = Journal(settings_path(native=True).parent / 'screen-display.json')
    if journal.load() is not None: raise RuntimeError('recover existing native display ownership first')
    original = NativeDisplay().read().transform
    startup = LoginStartup(startup_command())
    registered = NativeRunRegistry().read()
    activation = WindowActivation(settings_path(native=True).parent)
    if not activation.acquire(background=True): raise RuntimeError('native UI already running')
    app = QApplication([])
    app.setStyle('Fusion')
    window = None
    resident = None

    def wait(condition, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            QTest.qWait(50)
            if condition(): return
        raise RuntimeError('native resident exercise did not reach expected state')

    try:
        window = Window(native_backend=True, language='zh_CN', settings_path=root / 'settings.json', startup=startup)
        resident = ResidentTray(window, activation=activation)
        assert resident.enabled and resident.tray.isVisible()
        window.show()
        wait(lambda: bool(window.home.model.state and window.home.model.state['sensor_error'] is None))
        state = window.home.model.state
        assert state['native_backend'] and state['sensor_source'] == 'windows-winrt' and not state['simulation']
        process = window.client.process
        window.close()
        QTest.qWait(400)
        assert not window.isVisible()
        assert process.poll() is None and window.backend_timer.isActive()
        command = [sys.executable, '-m', 'panasonic_posture.fluent', '--native-backend']
        login_environment = dict(os.environ)
        login_environment.pop('PYTHONPATH', None)
        duplicate = subprocess.Popen(startup_command(), env=login_environment, cwd=root,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        wait(lambda: duplicate.poll() is not None, 8)
        assert duplicate.returncode == 0
        assert not window.isVisible()
        reopened = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        wait(lambda: reopened.poll() is not None, 8)
        assert reopened.returncode == 0
        wait(window.isVisible, 3)
        assert window.client.process.pid == process.pid
        window.switchTo(window.settings)
        QTest.qWait(250)
        card_y = window.settings.startup_card.mapTo(window.settings.widget(), window.settings.startup_card.rect().topLeft()).y()
        window.settings.verticalScrollBar().setValue(max(0, card_y - 160))
        QTest.qWait(100)
        window.grab().save(str(root / 'background-settings.png'))
        resident.quit_action.trigger()
        QTest.qWait(100)
        assert process.poll() == 0
        assert journal.load() is None
        assert NativeDisplay().read().transform == original
        assert NativeRunRegistry().read() == registered
        (root / 'result.json').write_text(json.dumps(dict(real_backend=True,
            native_tray_requested=True, close_retained_backend=True, background_duplicate_stayed_hidden=True,
            second_launch_reopened_same_process=True, explicit_quit_reaped_backend=True,
            public_cli_relaunch_exercised=True,
            startup_entry_without_source_environment_exercised=True,
            display_restored=True, startup_registration_changed=False,
            startup_command_units=len(startup.command.encode('utf-16-le')) // 2,
            notification_visual_or_login_exercised=False), indent=2), encoding='utf-8')
        print('Native resident hide/reopen, same-user activation, explicit quit and unchanged startup registration passed')
    finally:
        if resident is not None:
            resident.shutdown()
        elif window is not None:
            window.shutdown()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        activation.close()


if __name__ == '__main__': main()
