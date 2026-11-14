"""Explicit read-only WinRT accelerometer diagnostics, without input access.

Uses the Windows PowerShell WinRT projection already present in Windows 10.
It neither changes ReportInterval nor opens HID/input device handles. Samples
are diagnostic records, not engine-clock samples or evidence of mode execution.
"""
import argparse
import base64
from datetime import datetime
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from panasonic_posture.windows.ipc.frames import decode, ProtocolError

STANDARD_GRAVITY = 9.80665

_SCRIPT = r'''
$ErrorActionPreference='Stop'
$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
[Windows.Devices.Sensors.Accelerometer,Windows.Devices.Sensors,ContentType=WindowsRuntime] | Out-Null
$sensor=[Windows.Devices.Sensors.Accelerometer]::GetDefault()
if ($null -eq $sensor) { throw 'Windows accelerometer is unavailable' }
for ($index=0; $index -lt __SAMPLES__; $index++) {
    $reading=$sensor.GetCurrentReading()
    if ($null -eq $reading) { throw 'Windows accelerometer returned no sample' }
    [ordered]@{
        version=1
        device=$sensor.DeviceId
        minimum_report_interval_ms=$sensor.MinimumReportInterval
        timestamp=$reading.Timestamp.ToString('o')
        x=$reading.AccelerationX
        y=$reading.AccelerationY
        z=$reading.AccelerationZ
    } | ConvertTo-Json -Compress
    if ($index -lt (__SAMPLES__ - 1)) { Start-Sleep -Milliseconds __INTERVAL__ }
}
'''


class SensorProbeError(ValueError):
    pass


def parse_sample(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise SensorProbeError('invalid sensor sample version')
    device, stamp = value.get('device'), value.get('timestamp')
    interval = value.get('minimum_report_interval_ms')
    if not isinstance(device, str) or not 0 < len(device) <= 2048:
        raise SensorProbeError('invalid sensor identity')
    if type(interval) is not int or not 0 <= interval < 2**32:
        raise SensorProbeError('invalid minimum report interval')
    if not isinstance(stamp, str) or not 0 < len(stamp) <= 128:
        raise SensorProbeError('invalid sensor timestamp')
    try:
        if datetime.fromisoformat(stamp).tzinfo is None:
            raise ValueError('timezone missing')
    except ValueError as error:
        raise SensorProbeError('invalid sensor timestamp') from error
    vector = [value.get(axis) for axis in ('x', 'y', 'z')]
    if any(type(component) not in (int, float) or abs(component) > 1e6
           or not math.isfinite(component) for component in vector):
        raise SensorProbeError('invalid acceleration vector')
    normalized = [float(component) * STANDARD_GRAVITY for component in vector]
    return dict(device=device, timestamp=stamp, minimum_report_interval_ms=interval,
                acceleration_g=vector, acceleration_m_s2=normalized,
                magnitude_m_s2=math.sqrt(sum(component**2 for component in normalized)))


def probe(*, samples: int = 5, interval_ms: int = 200) -> list[dict]:
    if type(samples) is not int or not 1 <= samples <= 100:
        raise SensorProbeError('samples must be between 1 and 100')
    if type(interval_ms) is not int or not 10 <= interval_ms <= 1000:
        raise SensorProbeError('interval must be between 10 and 1000 milliseconds')
    if sys.platform != 'win32':
        raise SensorProbeError('run this read-only probe inside Windows')
    program = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = _SCRIPT.replace('__SAMPLES__', str(samples)).replace('__INTERVAL__', str(interval_ms))
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    try:
        result = subprocess.run([str(program), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            stdin=subprocess.DEVNULL, capture_output=True,
            timeout=60 + samples * interval_ms / 1000,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SensorProbeError('could not read the Windows accelerometer') from error
    if result.returncode:
        raise SensorProbeError('Windows did not provide accelerometer samples')
    if len(result.stdout) > 512 * 1024:
        raise SensorProbeError('sensor output exceeds the diagnostic limit')
    frames = result.stdout.splitlines()
    if len(frames) != samples:
        raise SensorProbeError('sensor sample count mismatch')
    try:
        readings = [parse_sample(decode(frame + b'\n')) for frame in frames]
    except ProtocolError as error:
        raise SensorProbeError('invalid sensor output') from error
    if len({reading['device'] for reading in readings}) != 1:
        raise SensorProbeError('sensor identity changed during the probe')
    return readings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--interval-ms', type=int, default=200)
    options = parser.parse_args(argv)
    try:
        readings = probe(samples=options.samples, interval_ms=options.interval_ms)
    except SensorProbeError as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(dict(version=1, source='windows-winrt', sample_count=len(readings),
        distinct_timestamps=len({reading['timestamp'] for reading in readings}), samples=readings),
        ensure_ascii=True, allow_nan=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
