"""Exercise Windows lock and display-power context without suspending the PC."""
import argparse
import ctypes as C
import json
from pathlib import Path
import sys
import time

from panasonic_posture.windows.security import WindowsSecurity
from panasonic_posture.windows.session_context import WindowsSessionContext


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--exercise-transitions', action='store_true', required=True)
    parser.add_argument('--directory', type=Path, required=True)
    options = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('run on an authorized Windows machine')
    target = options.directory.resolve()
    target.mkdir(parents=True, exist_ok=False)
    WindowsSecurity().require_unprivileged()
    user32 = C.WinDLL('user32', use_last_error=True)
    user32.LockWorkStation.argtypes = []
    user32.LockWorkStation.restype = C.c_int
    context = WindowsSessionContext()

    def wait(condition, seconds, message):
        deadline = time.monotonic() + seconds
        last = None
        while time.monotonic() < deadline:
            context.poll()
            last = context.snapshot()
            if condition(last):
                return last
            time.sleep(.1)
        raise RuntimeError(f'{message}; last={last!r}')

    try:
        initial = wait(
            lambda snap: snap['control_available'] and snap['display_on'] is True,
            10,
            'unlocked local session with display on was not ready',
        )
        if not user32.LockWorkStation():
            raise RuntimeError('LockWorkStation failed')
        locked = wait(
            lambda snap: (snap['presence'] is not None and snap['presence']['locked']
                          and not snap['control_available']),
            20,
            'lock transition was not observed',
        )
        print('Windows is locked. Unlock it to continue.', flush=True)
        unlocked = wait(
            lambda snap: (snap['presence'] is not None and not snap['presence']['locked']
                          and snap['control_available'] and snap['display_on'] is True),
            180,
            'unlock transition was not observed',
        )

        result = 0
        message = C.c_ulong()
        if not user32.SendMessageTimeoutW(
            0xFFFF, 0x0112, 0xF170, 2, 0x0002, 1000, C.byref(message)
        ):
            raise RuntimeError('display power-off request failed')
        display_off = wait(
            lambda snap: snap['display_on'] is False and snap['control_available'],
            15,
            'known display-off context was not observed',
        )
        user32.mouse_event(0x0001, 1, 1, 0, 0)
        display_on = wait(
            lambda snap: snap['display_on'] is True and snap['control_available'],
            20,
            'display did not wake from local pointer input',
        )
        evidence = dict(
            version=1,
            reads_only_after_requests=False,
            non_elevated=True,
            initial=initial,
            locked=locked,
            unlocked=unlocked,
            display_off=display_off,
            display_on=display_on,
            lock_transition=True,
            display_power_transition=True,
            known_display_off_preserved_control=True,
            suspend_exercised=False,
            app_backend_exercised=False,
        )
        (target / 'result.json').write_text(
            json.dumps(evidence, indent=2), encoding='utf-8')
        print(json.dumps(evidence, indent=2))
    finally:
        context.close()


if __name__ == '__main__':
    main()
