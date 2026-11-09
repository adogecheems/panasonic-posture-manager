"""Read-only native session/power observation on an authorized Windows host.

Does not unlock, suspend, wake, rotate or send notifications to the user.
It cannot prove transitions that did not occur while observation was running.
"""
import argparse
import json
from pathlib import Path
import sys
import time

from panasonic_posture.windows.security import WindowsSecurity
from panasonic_posture.windows.session_context import NativePowerSource, WindowsSessionContext


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observe-only', action='store_true', required=True)
    parser.add_argument('--seconds', type=float, default=3.)
    parser.add_argument('--directory', type=Path, required=True)
    options = parser.parse_args()
    if sys.platform != 'win32' or not 1 <= options.seconds <= 30:
        parser.error('use Windows and an observation duration from 1 to 30 seconds')
    target = options.directory.resolve()
    target.mkdir(parents=True, exist_ok=False)
    WindowsSecurity().require_unprivileged()
    context = WindowsSessionContext()
    snapshots = []
    previous = None
    try:
        deadline = time.monotonic() + options.seconds
        while time.monotonic() < deadline:
            context.poll()
            current = context.snapshot()
            if current != previous:
                snapshots.append(current)
                previous = current
            time.sleep(.05)
    finally:
        context.close()
    if NativePowerSource._live:
        raise RuntimeError('power notification registration remained open')
    observed = any(value['presence'] is not None and type(value['display_on']) is bool and
                   value['error'] is None for value in snapshots)
    result = dict(version=1, reads_only=True, non_elevated=True, registrations_closed=True,
                  observation_available=observed,
                  snapshots=snapshots, transitions_exercised=False)
    (target / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))
    if not observed:
        raise RuntimeError('no complete native session/power observation was obtained')


if __name__ == '__main__':
    main()
