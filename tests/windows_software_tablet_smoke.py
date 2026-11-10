"""Explicit four-direction software-tablet exercise on an authorized CFQV8.

The operator rotates the device and confirms that the screen remains upright.
The runner records the raw SimpleOrientation value, the Windows display
transform and the confirmed service snapshot for each direction.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.client import ProcessClient, settings_path
from panasonic_posture.windows.display import NativeDisplay, WindowsDisplay
from panasonic_posture.windows.orientation import SIMPLE_ORIENTATION_TRANSFORM
from panasonic_posture.windows.screen_session import require_native_user
from panasonic_posture.windows.sensor import IDENTITY, WindowsSensor


DIRECTIONS = (
    (0, 0, 'natural landscape'),
    (1, 3, 'rotated 90 degrees counter-clockwise'),
    (2, 2, 'upside down'),
    (3, 1, 'rotated 270 degrees counter-clockwise'),
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-software-tablet', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--watchdog', action='store_true')
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('requires an authorized Windows machine')
    directory = options.directory.resolve()
    journal = Journal(settings_path(native=True).parent / 'screen-display.json')
    heartbeat = directory / 'heartbeat'

    if options.watchdog:
        while not (directory / 'finished').exists():
            if not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > 10:
                WindowsDisplay(journal=journal).restore()
                return
            time.sleep(.2)
        return

    if directory.exists():
        raise RuntimeError('use a new evidence directory')
    directory.mkdir(parents=True)
    require_native_user()
    api = NativeDisplay()
    original = api.read()
    if original.transform != 0:
        raise RuntimeError('begin in normal landscape orientation')
    if journal.load() is not None:
        raise RuntimeError('recover the existing display journal first')

    stop_beat = threading.Event()
    beat = threading.Thread(target=lambda: _heartbeat(heartbeat, stop_beat), daemon=True)
    beat.start()
    watchdog = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()),
         '--exercise-software-tablet', '--directory', str(directory), '--watchdog'],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    client = sensor = None
    states = []
    result = {}
    try:
        client = ProcessClient(directory / 'settings.json', native=True, timeout=8)
        sensor = WindowsSensor(mount_matrix=IDENTITY, include_orientation=True)
        sensor.start()
        client.start(states.append, lambda: None)

        def wait(condition, timeout=45.):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                heartbeat.touch()
                client.poll()
                if condition():
                    return
                time.sleep(.02)
            raise RuntimeError('software-tablet smoke condition timed out')

        def applied(mode):
            return bool(states and (states[-1]['applied'] or {}).get('mode') == mode)

        def complete(future, timeout=8.):
            deadline = time.monotonic() + timeout
            while not future.done():
                if time.monotonic() >= deadline:
                    raise RuntimeError('native backend request timed out')
                heartbeat.touch()
                client.poll()
                time.sleep(.02)
            return future.result()

        wait(lambda: applied('laptop'))
        if states[-1]['hardware'] is not False:
            raise RuntimeError('open the device to laptop posture before rotating it')
        complete(client.set_mode('tablet'))
        wait(lambda: applied('tablet'))

        rows = []
        for raw, transform, label in DIRECTIONS:
            print(f'\nPlace the device {label}.')
            print('Keep the screen facing you and make sure it is stable.')
            input('Press Enter when ready: ')
            wait(lambda raw=raw: (sensor.orientation() is not None and
                                  sensor.orientation().value == raw))
            wait(lambda transform=transform: api.read().transform == transform)
            wait(lambda: applied('tablet'))
            if states[-1]['error_code']:
                raise RuntimeError(f"tablet state reported {states[-1]['error_code']}")
            answer = input('Does the screen appear upright and readable? [y/N] ').strip().lower()
            if answer not in ('y', 'yes'):
                raise RuntimeError(f'operator rejected {label}')
            rows.append(dict(
                direction=label,
                raw_orientation=raw,
                expected_transform=transform,
                observed_transform=api.read().transform,
                screen_upright=True,
                applied=states[-1]['applied'],
                input_control_available=states[-1]['input_control_available'],
            ))

        complete(client.set_mode('laptop'))
        wait(lambda: applied('laptop'))
        child = client.process
        client.stop()
        client = None
        if child.poll() != 0:
            raise RuntimeError('native backend did not shut down cleanly')
        if api.read().transform != original.transform or journal.load() is not None:
            raise RuntimeError('normal exit did not restore the original display')
        result = dict(version=1, directions=rows, physical_fold_exercised=False,
                      input_control_exercised=False, normal_exit_restored=True)
    finally:
        stop_beat.set()
        if sensor is not None:
            sensor.close()
        if client is not None:
            client.stop()
        WindowsDisplay(journal=journal).restore()
        heartbeat.touch()
        (directory / 'finished').touch()
        watchdog.wait(timeout=5)
        beat.join(timeout=2)

    (directory / 'result.json').write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Four-direction software-tablet rotation and normal-exit restoration passed')


def _heartbeat(path: Path, stop: threading.Event) -> None:
    while not stop.wait(1.):
        path.touch()


if __name__ == '__main__':
    main()
