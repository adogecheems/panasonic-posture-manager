"""Explicit real screen/backend exercise on an authorized Windows machine.

Uses temporary test preferences and the production per-user display journal.
A separate watchdog restores journalled ownership if this runner stops. This
does not exercise the physical fold switch or claim any input filtering.
"""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.client import ProcessClient, settings_path
from panasonic_posture.windows.display import NativeDisplay, WindowsDisplay


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-screen', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--watchdog', action='store_true')
    parser.add_argument('--locked-session', action='store_true', help='verify locked-session refusal without rotating')
    options = parser.parse_args()
    if sys.platform != 'win32': parser.error('requires an authorized Windows machine')
    directory = options.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    heartbeat = directory / 'heartbeat'
    journal = Journal(settings_path(native=True).parent / 'screen-display.json')
    if options.watchdog:
        while not (directory / 'finished').exists():
            if not heartbeat.exists() or time.time() - heartbeat.stat().st_mtime > 10:
                WindowsDisplay(journal=journal).restore()
                return
            time.sleep(.2)
        return
    if (directory / 'result.json').exists(): raise RuntimeError('use a new evidence directory')
    api = NativeDisplay()
    original = api.read()
    if original.transform != 0: raise RuntimeError('begin in normal landscape orientation')
    if journal.load() is not None: raise RuntimeError('recover the existing display journal first')
    states, lost = [], []
    client = ProcessClient(directory / 'settings.json', native=True, timeout=8)
    heartbeat.touch()
    watchdog = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--exercise-screen',
        '--directory', str(directory), '--watchdog'], creationflags=subprocess.CREATE_NO_WINDOW)

    def wait(condition, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            heartbeat.touch()
            client.poll()
            if condition(): return
            time.sleep(.02)
        raise RuntimeError('native screen pipeline did not reach expected state')

    def mode(name): return bool(states and (states[-1]['applied'] or {}).get('mode') == name)
    def request(method, *args):
        future = getattr(client, method)(*args)
        wait(future.done)
        future.result()

    result = {}
    try:
        client.start(states.append, lambda: lost.append(True))
        if options.locked_session:
            wait(lambda: bool(states and states[-1]['sensor_error'] is None and states[-1]['hardware'] is False))
            assert states[-1]['applied'] is None
            assert states[-1]['session_connected'] is False
            request('set_mode', 'tent')
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                wait(lambda: True)
                assert states[-1]['applied'] is None
                assert api.read().transform == original.transform
                time.sleep(.05)
            result.update(locked_session_rejected=True, initial=states[-1],
                          physical_fold_exercised=False, input_control_exercised=False)
            child = client.process
            client.stop()
            assert child.poll() == 0, 'native shutdown did not finish gracefully'
            assert journal.load() is None
            result['normal_exit_completed'] = True
            (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Native fresh sensor/switch/IPC, locked-session refusal and graceful exit passed; rotation not exercised')
            return
        wait(lambda: mode('laptop'))
        assert states[-1]['sensor_source'] == 'windows-winrt'
        assert states[-1]['sensor_error'] is None
        assert states[-1]['simulation'] is False
        assert states[-1]['input_control_available'] is False
        result['initial'] = states[-1]
        request('set_preference', 'stand_prompt', True)
        assert json.loads((directory / 'settings.json').read_text(encoding='utf-8'))['stand_prompt']
        request('calibrate')
        assert states[-1]['sensor_calibrated']
        request('reset_calibration')
        assert not states[-1]['sensor_calibrated']
        request('set_mode', 'tent')
        wait(lambda: mode('tent'))
        assert api.read().transform == 2
        assert states[-1]['applied']['disable_internal_input'] is False
        result['tent'] = states[-1]
        request('set_mode', 'stand')
        wait(lambda: mode('stand'))
        assert api.read().transform == 0
        request('set_mode', 'tent')
        wait(lambda: mode('tent'))
        child = client.process
        client.stop()
        assert child.poll() == 0, 'normal shutdown did not finish gracefully'
        assert api.read().transform == original.transform
        assert journal.load() is None
        result['normal_exit_restored'] = True
        client.start(states.append, lambda: lost.append(True))
        wait(lambda: bool(states and states[-1]['instance'] != result['tent']['instance'] and mode('laptop')))
        request('set_mode', 'tent')
        wait(lambda: mode('tent'))
        old_instance = states[-1]['instance']
        client.process.kill()
        client.process.wait(timeout=3)
        wait(lambda: bool(states and states[-1]['instance'] != old_instance and mode('laptop')))
        assert api.read().transform == 0
        result['forced_exit_reconnect_recovered'] = True
        result['restored'] = asdict(api.read())
        result['physical_fold_exercised'] = False
        result['input_control_exercised'] = False
    finally:
        heartbeat.touch()
        client.stop()
        WindowsDisplay(journal=journal).restore()
        (directory / 'finished').touch()
        watchdog.wait(timeout=3)
    assert api.read().transform == original.transform
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Native sensor/backend/display, preferences/calibration, normal exit and crash recovery passed')


if __name__ == '__main__': main()
