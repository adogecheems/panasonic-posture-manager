"""Exercise only the Windows internal-display transaction on real hardware."""
from dataclasses import asdict
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

from panasonic_posture.model import Target
from panasonic_posture.session.journal_store import Journal
from panasonic_posture.windows.display import WindowsDisplay
from panasonic_posture.windows.instance import InstanceLock


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-display', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--watchdog', action='store_true')
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('run on an authorized Windows machine')
    directory = options.directory.resolve()
    heartbeat = directory / 'heartbeat'
    journal = Journal(directory / 'display.json')

    if options.watchdog:
        while not (directory / 'finished').exists():
            if not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > 10:
                WindowsDisplay(journal=journal).restore()
                return
            time.sleep(.2)
        return
    if options.worker:
        display = WindowsDisplay(journal=journal)
        display.apply(Target(
            mode='tent', tablet=True, rotation=180, disable_internal_input=False,
        ))
        (directory / 'worker-ready.json').write_text(
            json.dumps(asdict(display.api.read())), encoding='utf-8')
        try:
            time.sleep(60)
        finally:
            display.restore()
        return

    if directory.exists():
        raise RuntimeError('use a new evidence directory')
    directory.mkdir(parents=True)
    with InstanceLock(directory / 'controller'):
        heartbeat.touch()
        command = [
            sys.executable, str(Path(__file__).resolve()),
            '--exercise-display', '--directory', str(directory),
        ]
        flags = subprocess.CREATE_NO_WINDOW
        watchdog = subprocess.Popen(
            command + ['--watchdog'], creationflags=flags)
        worker = None
        display = WindowsDisplay(journal=journal)
        original = display.api.read()
        result = {}
        try:
            if original.transform == 2:
                raise RuntimeError('begin outside flipped landscape')
            if journal.load() is not None:
                raise RuntimeError('recover the existing display journal first')

            display.apply(Target(
                mode='tent', tablet=True, rotation=180, disable_internal_input=False,
            ))
            tent = display.api.read()
            if tent.transform != 2:
                raise RuntimeError('tent rotation was not applied')
            display.restore()
            restored = display.api.read()
            if restored != original or journal.load() is not None:
                raise RuntimeError('normal display restore did not complete')

            worker = subprocess.Popen(
                command + ['--worker'], creationflags=flags)
            marker = directory / 'worker-ready.json'
            deadline = time.monotonic() + 10
            while (not marker.exists() and worker.poll() is None
                   and time.monotonic() < deadline):
                heartbeat.touch()
                time.sleep(.05)
            if not marker.exists():
                raise RuntimeError('display worker did not reach applied state')
            if display.api.read().transform != 2:
                raise RuntimeError('display worker readback did not match')
            worker.kill()
            worker.wait(timeout=3)
            recovered = WindowsDisplay(journal=journal)
            if recovered.api.read() != original or journal.load() is not None:
                raise RuntimeError('forced worker recovery did not restore display')

            result = dict(
                original=asdict(original),
                tent=asdict(tent),
                restored=asdict(restored),
                normal_restore=True,
                forced_worker_recovery=True,
                journal_cleared=True,
                slate_indicator_exercised=False,
                input_control_exercised=False,
            )
            (directory / 'result.json').write_text(
                json.dumps(result, indent=2), encoding='utf-8')
            print(json.dumps(result, indent=2))
        finally:
            if worker.poll() is None:
                worker.kill()
                worker.wait(timeout=3)
            heartbeat.touch()
            WindowsDisplay(journal=journal).restore()
            (directory / 'finished').touch()
            watchdog.wait(timeout=3)


if __name__ == '__main__':
    main()
