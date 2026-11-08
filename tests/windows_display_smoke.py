"""Explicit native-display exercise on an authorized Windows test machine.

Temporarily rotates the unique built-in panel. Uses a persistent ownership
journal and an independent recovery process; never changes display registry
settings, automatic-rotation preferences or external monitor orientations.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.display import NativeDisplay, WindowsDisplay
from panasonic_posture.windows.instance import InstanceLock


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
        if original.transform == 2:
            raise RuntimeError('begin in an orientation other than flipped landscape')
        watchdog = subprocess.Popen(command + ['--watchdog'], creationflags=flags)
        try:
            display.apply(dict(mode='tent', tablet=True, rotation=180, disable_internal_input=True))
            assert display.api.read().transform == 2
            assert journal.load()['written_transform'] == 2
            time.sleep(1)
            display.restore()
            assert display.api.read().transform == original.transform
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
                rotation_readback=True, normal_restore=True, killed_worker_recovery=True),
                indent=2), encoding='utf-8')
            print('Native internal-panel rotation, readback, restore and killed-worker recovery passed')
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
