"""Exercise real folded-tablet hardware priority and display handoff."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.client import ProcessClient, settings_path
from panasonic_posture.windows.display import NativeDisplay, WindowsDisplay


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-physical', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('run on an authorized Windows machine')
    directory = options.directory.resolve()
    if directory.exists():
        raise RuntimeError('use a new evidence directory')
    directory.mkdir(parents=True)
    journal = Journal(settings_path(native=True).parent / 'screen-display.json')
    if journal.load() is not None:
        raise RuntimeError('recover the existing display journal first')

    display = WindowsDisplay(journal=journal)
    original = display.api.read()
    client = ProcessClient(directory / 'settings.json', native=True, timeout=8)
    states = []
    try:
        client.start(states.append, lambda: None)

        def wait(condition, timeout, message):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                client.poll()
                if states and condition(states[-1]):
                    return states[-1]
                time.sleep(.05)
            raise RuntimeError(message)

        laptop = wait(
            lambda state: (state['hardware'] is False and
                           (state['applied'] or {}).get('mode') == 'laptop'),
            45,
            'native backend did not reach confirmed laptop mode',
        )
        print('Fold the device completely into tablet use and keep it stable.',
              flush=True)
        folded = wait(
            lambda state: (state['hardware'] is True and
                           (state['applied'] or {}).get('mode') == 'hardware'),
            300,
            'physical tablet state was not confirmed',
        )
        before_rotation = display.api.read()
        input('Rotate the device in tablet use, confirm the screen is upright, then press Enter: ')
        client.refresh()

        def moved(state):
            return (state['hardware'] is True and
                    (state['applied'] or {}).get('mode') == 'hardware' and
                    not state['error_code'])

        rotated = wait(moved, 15, 'rotated tablet state was not stable')
        after_rotation = display.api.read()
        print('Unfold the device back to laptop use and keep it stable.', flush=True)
        unfolded = wait(
            lambda state: (state['hardware'] is False and
                           (state['applied'] or {}).get('mode') == 'laptop'),
            300,
            'physical laptop state was not confirmed',
        )
        child = client.process
        client.stop()
        if child.poll() != 0:
            raise RuntimeError('native backend did not shut down cleanly')
        restored = display.api.read()
        if restored != original or journal.load() is not None:
            raise RuntimeError('normal exit did not restore the original display')
        result = dict(
            version=1,
            laptop=laptop,
            folded=folded,
            rotated=rotated,
            unfolded=unfolded,
            before_rotation=asdict(before_rotation),
            after_rotation=asdict(after_rotation),
            rotation_observed=before_rotation.transform != after_rotation.transform,
            normal_exit_restored=True,
            manual_tablet_exercised=False,
            input_control_exercised=False,
        )
        (directory / 'result.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(result, indent=2))
    finally:
        client.stop()
        WindowsDisplay(journal=journal).restore()


if __name__ == '__main__':
    main()
