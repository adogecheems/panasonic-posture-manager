"""Read-only LocalSystem probe for HID Event Filter ACPI notifications.

Run this explicitly as LocalSystem, fold/unfold the machine during the probe,
and inspect the resulting JSON.  The script never writes CSM or changes shell
mode.  A missing event after a physical movement means this provider is not a
usable no-driver physical source on that machine.
"""
import argparse
import ctypes as C
import json
from pathlib import Path
import sys
import threading
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / 'src'))

from panasonic_posture.windows.hardware.etw_switch import (
    WindowsHidEventFilterBackend, parse_acpi_notification,
)
from panasonic_posture.windows.common.security import WindowsSecurity


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=30.)
    parser.add_argument('--session-name', default='PanasonicPostureManager.PhysicalTablet.Probe.v1')
    options = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('HID Event Filter ETW requires Windows')
    if not 1 <= options.seconds <= 300:
        parser.error('--seconds must be between 1 and 300')
    options.directory.mkdir(parents=True, exist_ok=True)
    result = {
        'success': False,
        'event_id': 115,
        'provider': 'HidEventFilter',
        'samples': [],
        'csm_changes': [],
    }
    lock = threading.Lock()
    backend = None
    try:
        WindowsSecurity().require_system()
        backend = WindowsHidEventFilterBackend(session_name=options.session_name)
        user32 = C.WinDLL('user32')
        user32.GetSystemMetrics.argtypes = [C.c_int]
        user32.GetSystemMetrics.restype = C.c_int
        csm = lambda: user32.GetSystemMetrics(0x2003)
        current_csm = csm()
        result['initial_csm'] = current_csm

        def received(data):
            sample = {
                'received_at': time.time(),
                'data_hex': data.hex(),
                'physical_tablet': parse_acpi_notification(data),
            }
            with lock:
                result['samples'].append(sample)

        backend.start(received)
        deadline = time.monotonic() + options.seconds
        while time.monotonic() < deadline:
            if backend.failure is not None:
                raise OSError('ETW consumer failed') from backend.failure
            observed = csm()
            if observed != current_csm:
                current_csm = observed
                with lock:
                    result['csm_changes'].append({
                        'observed_at': time.time(),
                        'csm': observed,
                    })
            time.sleep(.05)
        result['final_csm'] = current_csm
        result['success'] = True
    except BaseException as error:
        result['error'] = str(error)
        result['traceback'] = traceback.format_exc()
    finally:
        if backend is not None:
            try:
                backend.close()
            except (OSError, ValueError) as error:
                result['close_error'] = str(error)
        (options.directory / 'windows-etw-switch-result.json').write_text(
            json.dumps(result, indent=2), encoding='utf-8')
    return 0 if result['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
