"""Read-only native proof that a killed parent cannot orphan its WinRT helper."""
import argparse
import ctypes as C
import json
from pathlib import Path
import subprocess
import sys
import time

from panasonic_posture.windows.hardware.sensor import WindowsSensor, PowerShellSource, IDENTITY


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--child', action='store_true')
    parser.add_argument('--orientation', action='store_true')
    options = parser.parse_args()
    if sys.platform != 'win32': parser.error('requires Windows')
    directory = options.directory.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if options.child:
        def source():
            port = PowerShellSource(include_orientation=options.orientation)
            (directory / 'helper.json').write_text(json.dumps(dict(pid=port.process.pid)), encoding='utf-8')
            return port
        sensor = WindowsSensor(mount_matrix=IDENTITY, source_factory=source, include_orientation=options.orientation)
        sensor.start()
        try:
            while True:
                if sensor.reading().valid and (not options.orientation or sensor.orientation() is not None):
                    (directory / 'fresh').touch()
                time.sleep(.05)
        finally:
            sensor.close()
    if (directory / 'helper.json').exists(): raise RuntimeError('use a new evidence directory')
    command = [sys._base_executable, str(Path(__file__).resolve()), '--directory', str(directory), '--child']
    if options.orientation:
        command.append('--orientation')
    child = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
    kernel = C.WinDLL('kernel32', use_last_error=True, winmode=0x800)
    kernel.OpenProcess.argtypes, kernel.OpenProcess.restype = [C.c_uint32, C.c_int32, C.c_uint32], C.c_void_p
    kernel.WaitForSingleObject.argtypes, kernel.WaitForSingleObject.restype = [C.c_void_p, C.c_uint32], C.c_uint32
    kernel.TerminateProcess.argtypes, kernel.TerminateProcess.restype = [C.c_void_p, C.c_uint32], C.c_int32
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [C.c_void_p], C.c_int32
    handle = None
    try:
        deadline = time.monotonic() + 25
        while not (directory / 'fresh').exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        if not (directory / 'fresh').exists(): raise RuntimeError('native sensor did not produce a fresh sample')
        helper = json.loads((directory / 'helper.json').read_text(encoding='utf-8'))['pid']
        handle = kernel.OpenProcess(0x100001, False, helper)
        if not handle or kernel.WaitForSingleObject(handle, 0) != 258:
            raise RuntimeError('native helper was not live before forced parent exit')
        child.kill()
        child.wait(timeout=3)
        if kernel.WaitForSingleObject(handle, 6000) != 0:
            raise RuntimeError('WinRT helper survived its exact parent instance')
        (directory / 'result.json').write_text(json.dumps(dict(fresh_native_sample=True,
            coarse_direction=options.orientation, parent_forcibly_stopped=True,
            helper_exited=True, display_or_input_written=False), indent=2), encoding='utf-8')
        print('Fresh native sensor sample and exact-parent forced-exit helper cleanup passed')
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        if handle:
            if kernel.WaitForSingleObject(handle, 1000) != 0:
                kernel.TerminateProcess(handle, 1)
                kernel.WaitForSingleObject(handle, 3000)
            kernel.CloseHandle(handle)


if __name__ == '__main__': main()
