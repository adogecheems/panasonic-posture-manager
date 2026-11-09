"""Explicit native-display exercise on an authorized Windows test machine.

Temporarily rotates the unique built-in panel and exercises Windows shell
modes. Uses a persistent ownership journal and an independent recovery
process; restores the original lock and shell settings and never changes
display registry settings or external monitor orientations.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.display import WindowsDisplay
from panasonic_posture.windows.instance import InstanceLock
from panasonic_posture.windows.tablet_mode import DESKTOP, TABLET, WindowsTabletMode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-rotation', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--watchdog', action='store_true')
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('run on an authorized Windows machine')
    directory = options.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    journal = Journal(directory / 'display.json')
    if options.watchdog:
        time.sleep(20)
        WindowsDisplay(journal=journal).restore()
        return
    if options.worker:
        display = WindowsDisplay(journal=journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
        (directory / 'worker-ready.json').write_text(json.dumps(asdict(display.api.read())), encoding='utf-8')
        time.sleep(60)
        display.restore()
        return
    with InstanceLock(directory / 'exercise'):
        marker = directory / 'worker-ready.json'
        if marker.exists():
            raise RuntimeError('use a new evidence directory for each exercise')
        command = [sys.executable, str(Path(__file__).resolve()), '--exercise-rotation',
                   '--directory', str(directory)]
        flags = subprocess.CREATE_NO_WINDOW
        worker = None
        display = WindowsDisplay(journal=journal)
        original = display.api.read()
        shell = WindowsTabletMode()
        original_shell = shell.read()
        if original.transform == 2:
            raise RuntimeError('begin in an orientation other than flipped landscape')
        watchdog = subprocess.Popen(command + ['--watchdog'], creationflags=flags)
        try:
            modes = []
            for mode, transform, shell_mode in (
                    ('tent', 2, TABLET), ('stand', 0, TABLET),
                    ('laptop', 0, DESKTOP)):
                display.apply(dict(mode=mode, tablet=mode == 'tent',
                                   rotation=180 if mode == 'tent' else 0,
                                   disable_internal_input=False))
                assert display.api.read().transform == transform
                assert display._read_state().shell_mode == shell_mode
                modes.append(dict(mode=mode, transform=transform, shell_mode=shell_mode))
            time.sleep(1)
            display.restore()
            assert display.api.read().transform == original.transform
            assert display.api.read().auto_rotation == original.auto_rotation
            assert shell.read() == original_shell
            assert journal.load() is None
            worker = subprocess.Popen(command + ['--worker'], creationflags=flags)
            deadline = time.monotonic() + 8
            while not marker.exists() and worker.poll() is None and time.monotonic() < deadline:
                time.sleep(.05)
            if not marker.exists():
                raise RuntimeError('native transaction worker failed or timed out')
            assert display.api.read().transform == 2
            worker.kill()
            worker.wait(timeout=3)
            WindowsDisplay(journal=journal)
            assert display.api.read().transform == original.transform
            assert journal.load() is None
            (directory / 'result.json').write_text(json.dumps(dict(
                original=asdict(original), restored=asdict(display.api.read()),
                original_shell_mode=original_shell, modes=modes,
                rotation_readback=True, shell_readback=True, lock_restore=True,
                normal_restore=True, killed_worker_recovery=True),
                indent=2), encoding='utf-8')
            print('Native panel, direction lock, shell mode, restore and killed-worker recovery passed')
        finally:
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.wait(timeout=3)
            # Stop the watchdog only after the ownership journal is cleared.
            # If this cleanup fails it remains alive to make another attempt.
            WindowsDisplay(journal=journal).restore()
            if journal.load() is None:
                watchdog.terminate()
                watchdog.wait(timeout=3)


if __name__ == '__main__':
    main()
