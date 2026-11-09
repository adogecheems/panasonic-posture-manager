"""Explicit native-display and convertible-slate exercise on this test PC.

Temporarily rotates the unique built-in panel and reports the verified
laptop/slate indicator while recording Windows' own shell response. It never
calls the shell mode setter or changes the user's tablet-mode preference.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys
import time

from panasonic_posture.model import Target
from panasonic_posture.session.journal import Journal
from panasonic_posture.windows.convertible_slate import NativeSlateDevice, SlateNotifier
from panasonic_posture.windows.display import WindowsDisplay
from panasonic_posture.windows.instance import InstanceLock
from panasonic_posture.windows.session_display import QualifiedDisplay
from panasonic_posture.windows.slate_journal import SlateJournal, lease_path
from panasonic_posture.windows.tablet_mode import WindowsShellMode, read_prompt_preference


class SmokeContext:
    def control_available(self):
        return True

    def poll(self):
        pass

    def close(self):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-rotation', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--slate-worker', action='store_true')
    parser.add_argument('--watchdog', action='store_true')
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('run on an authorized Windows machine')
    directory = options.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    journal = Journal(directory / 'display.json')
    slate_journal = SlateJournal(lease_path(journal.path))

    if options.watchdog:
        time.sleep(20)
        WindowsDisplay(journal=journal).restore()
        return

    if options.slate_worker:
        device = NativeSlateDevice()
        notifier = SlateNotifier(device, slate_journal)
        notifier.sync_mode('tent')
        (directory / 'slate-worker-ready.json').write_text(
            json.dumps(dict(slate=device.read())), encoding='utf-8'
        )
        time.sleep(60)
        notifier.close()
        return

    if options.worker:
        display = WindowsDisplay(journal=journal)
        display.apply(dict(mode='tent', tablet=True, rotation=180,
                           disable_internal_input=False))
        (directory / 'worker-ready.json').write_text(
            json.dumps(asdict(display.api.read())), encoding='utf-8'
        )
        time.sleep(60)
        display.restore()
        return

    with InstanceLock(directory / 'exercise'):
        marker = directory / 'worker-ready.json'
        slate_marker = directory / 'slate-worker-ready.json'
        if marker.exists() or slate_marker.exists():
            raise RuntimeError('use a new evidence directory for each exercise')
        command = [sys.executable, str(Path(__file__).resolve()),
                   '--exercise-rotation', '--directory', str(directory)]
        flags = subprocess.CREATE_NO_WINDOW
        worker = slate_worker = None
        display = WindowsDisplay(journal=journal)
        original = display.api.read()
        slate_device = NativeSlateDevice()
        original_slate = slate_device.read()
        slate = SlateNotifier(slate_device, slate_journal)
        qualified = QualifiedDisplay(display, SmokeContext(), slate)
        shell = WindowsShellMode()
        original_shell = shell.read()
        preference = read_prompt_preference()
        if original.transform == 2:
            raise RuntimeError('begin in an orientation other than flipped landscape')
        if original_slate:
            raise RuntimeError('begin in laptop use so slate recovery can be verified')
        watchdog = subprocess.Popen(command + ['--watchdog'], creationflags=flags)
        try:
            modes = []
            for mode, transform, expected_slate in (
                    ('tent', 2, True), ('stand', 0, True),
                    ('laptop', 0, False)):
                qualified.apply(Target(
                    mode=mode,
                    tablet=expected_slate,
                    rotation=180 if mode == 'tent' else 0,
                    disable_internal_input=False,
                ))
                assert display.api.read().transform == transform
                assert slate_device.read() is expected_slate
                # Windows owns the response to the policy. Under the common
                # "do not switch" preference this remains desktop mode; under
                # automatic behavior the shell may switch without our help.
                observed_shell = shell.read()
                modes.append(dict(
                    mode=mode,
                    transform=transform,
                    slate_reported=expected_slate,
                    shell_mode=observed_shell,
                ))
            time.sleep(1)
            qualified.restore()
            assert display.api.read().transform == original.transform
            assert display.api.read().auto_rotation == original.auto_rotation
            assert slate_device.read() is original_slate
            assert journal.load() is None

            worker = subprocess.Popen(command + ['--worker'], creationflags=flags)
            deadline = time.monotonic() + 8
            while (not marker.exists() and worker.poll() is None
                   and time.monotonic() < deadline):
                time.sleep(.05)
            if not marker.exists():
                raise RuntimeError('native transaction worker failed or timed out')
            assert display.api.read().transform == 2
            worker.kill()
            worker.wait(timeout=3)
            WindowsDisplay(journal=journal)
            assert display.api.read().transform == original.transform
            assert journal.load() is None

            slate.close()
            slate_worker = subprocess.Popen(
                command + ['--slate-worker'], creationflags=flags
            )
            deadline = time.monotonic() + 8
            while (not slate_marker.exists() and slate_worker.poll() is None
                   and time.monotonic() < deadline):
                time.sleep(.05)
            if not slate_marker.exists():
                raise RuntimeError('convertible-slate worker failed or timed out')
            with NativeSlateDevice() as readback:
                assert readback.read() is True
            slate_worker.kill()
            slate_worker.wait(timeout=3)
            with NativeSlateDevice() as recovered_device:
                recovered = SlateNotifier(recovered_device, slate_journal)
                assert recovered_device.read() is True
                assert slate_journal.load() is not None
                recovered.sync_mode('laptop')
                assert recovered_device.read() is original_slate
                recovered.close()
            assert slate_journal.load() is None

            (directory / 'result.json').write_text(json.dumps(dict(
                original=asdict(original),
                restored=asdict(display.api.read()),
                original_shell_mode=original_shell,
                shell_preference=preference,
                original_slate=original_slate,
                modes=modes,
                rotation_readback=True,
                slate_readback=True,
                shell_policy_respected=True,
                lock_restore=True,
                slate_restore=True,
                normal_restore=True,
                rotation_worker_recovery=True,
                slate_worker_recovery=True,
            ), indent=2), encoding='utf-8')
            print('Native rotation, slate notification, normal restore, and '
                  'rotation/slate worker recovery passed')
        finally:
            if worker is not None and worker.poll() is None:
                worker.kill()
                worker.wait(timeout=3)
            if slate_worker is not None and slate_worker.poll() is None:
                slate_worker.kill()
                slate_worker.wait(timeout=3)
            qualified.restore()
            if slate_journal.load() is not None:
                recovery = SlateNotifier(NativeSlateDevice(), slate_journal)
                recovery.close()
            if journal.load() is None:
                watchdog.terminate()
                watchdog.wait(timeout=3)
            slate.close()


if __name__ == '__main__':
    main()
