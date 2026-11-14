"""Read-only WinRT acquisition, separate from the unadapted software backend.

The caller must supply a verified mount rotation before using this as an
engine SensorBackend. The diagnostic command uses identity axes explicitly;
it never starts an engine or grants device-control capabilities.
"""
from dataclasses import asdict, replace
from collections.abc import Callable, Sequence
from datetime import datetime
import argparse
import base64
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from panasonic_posture.calibration import apply_calibration, parse_calibration_matrix
from panasonic_posture.model import SensorInfo, SensorReading
from panasonic_posture.windows.hardware.probe import parse_sample, SensorProbeError
from panasonic_posture.windows.ipc.frames import MAX_FRAME, decode
from panasonic_posture.windows.hardware.orientation import (
    OrientationTracker,
    OrientationReading,
    OrientationError,
)

SAMPLE_MAX_AGE = 2.0
STARTUP_TIMEOUT = 20.0
RETRY_SECONDS = 2.0
CLOCK_TOLERANCE = .25
IDENTITY = (1., 0., 0., 0., 1., 0., 0., 0., 1.)

STREAM_SCRIPT = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$OutputEncoding=[Console]::OutputEncoding=[Text.UTF8Encoding]::new()
$parent=[Diagnostics.Process]::GetProcessById(__PARENT_PID__)
$null=$parent.Handle
if ($parent.StartTime.ToUniversalTime().ToFileTimeUtc() -ne __PARENT_START__) { throw 'Sensor parent identity changed' }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
[Windows.Devices.Sensors.Accelerometer,Windows.Devices.Sensors,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Sensors.AccelerometerReadingType,Windows.Devices.Sensors,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Enumeration.DeviceInformation,Windows.Devices.Enumeration,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Enumeration.DeviceInformationCollection,Windows.Devices.Enumeration,ContentType=WindowsRuntime] | Out-Null
$asTask=[System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
    $_.GetGenericArguments().Count -eq 1 -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
__ORIENTATION_SETUP__
$selector=[Windows.Devices.Sensors.Accelerometer]::GetDeviceSelector([Windows.Devices.Sensors.AccelerometerReadingType]::Standard)
$sensor=[Windows.Devices.Sensors.Accelerometer]::GetDefault([Windows.Devices.Sensors.AccelerometerReadingType]::Standard)
if ($null -eq $sensor) { throw 'Windows accelerometer is unavailable' }
$deviceId=$sensor.DeviceId
$ids=@()
$identityCheck=[Diagnostics.Stopwatch]::StartNew()
while (-not $parent.HasExited) {
    # Device enumeration is expensive. Keep the authenticated sensor object
    # and revalidate selection at the sample freshness horizon.
    if ($ids.Count -eq 0 -or $identityCheck.Elapsed.TotalSeconds -ge 2) {
        $operation=[Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($selector)
        $task=$asTask.MakeGenericMethod([Windows.Devices.Enumeration.DeviceInformationCollection]).Invoke($null,@($operation))
        if (-not $task.Wait(2000)) { throw 'Accelerometer enumeration timed out' }
        $ids=@($task.Result | ForEach-Object { $_.Id })
        if ($ids.Count -ne 1 -or $ids[0] -ne $deviceId) { throw 'Accelerometer identity is ambiguous or changed' }
        $identityCheck.Restart()
    }
    $reading=$sensor.GetCurrentReading()
    if ($null -eq $reading) { throw 'Windows accelerometer returned no sample' }
    $orientation=$null
    __ORIENTATION_READ__
    [ordered]@{
        version=1
        device=$deviceId
        candidates=$ids
        minimum_report_interval_ms=$sensor.MinimumReportInterval
        timestamp=$reading.Timestamp.ToString('o')
        observed_at=[DateTimeOffset]::UtcNow.ToString('o')
        x=$reading.AccelerationX
        y=$reading.AccelerationY
        z=$reading.AccelerationZ
        orientation=$orientation
    } | ConvertTo-Json -Compress -Depth 4
    [Console]::Out.Flush()
    Start-Sleep -Milliseconds 100
}
'''

ORIENTATION_SETUP = r'''
$orientationSensor=$null
$orientationTypeAvailable=$false
try {
    [Windows.Devices.Sensors.SimpleOrientationSensor,Windows.Devices.Sensors,ContentType=WindowsRuntime] | Out-Null
    $orientationTypeAvailable=$true
} catch {}
'''

ORIENTATION_READ = r'''
if ($orientationTypeAvailable) {
    try {
        if ($null -eq $orientationSensor) {
            $orientationSensor=[Windows.Devices.Sensors.SimpleOrientationSensor]::GetDefault()
            $orientationSelector=[Windows.Devices.Sensors.SimpleOrientationSensor]::GetDeviceSelector()
        }
        if ($null -eq $orientationSensor) { throw 'Simple orientation is unavailable' }
        $operation=[Windows.Devices.Enumeration.DeviceInformation]::FindAllAsync($orientationSelector)
        $task=$asTask.MakeGenericMethod([Windows.Devices.Enumeration.DeviceInformationCollection]).Invoke($null,@($operation))
        if (-not $task.Wait(2000)) { throw 'Simple orientation enumeration timed out' }
        $orientationIds=@($task.Result | ForEach-Object { $_.Id })
        if ($orientationIds.Count -ne 1 -or $orientationIds[0] -ne $orientationSensor.DeviceId) {
            throw 'Simple orientation identity is ambiguous or changed'
        }
        $orientation=[ordered]@{
            device=$orientationSensor.DeviceId
            candidates=$orientationIds
            value=[int]$orientationSensor.GetCurrentOrientation()
            reading_transform=[int]$orientationSensor.ReadingTransform
            observed_at=[DateTimeOffset]::UtcNow.ToString('o')
        }
    } catch {
        $orientationSensor=$null
        $orientation=[ordered]@{error='orientation-read-unavailable'}
    }
}
'''


class SensorError(ValueError):
    pass


class StaleSample(SensorError):
    """A cached frame may be followed by fresh data on the same connection."""


def _timestamp(value: object) -> float:
    if not isinstance(value, str) or len(value) > 128:
        raise SensorError('invalid sensor time')
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('timezone missing')
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError) as error:
        raise SensorError('invalid sensor time') from error


class SampleTracker:
    """Map sample time once; repeated cached readings cannot renew a lease."""

    def __init__(self, *, mount_matrix: Sequence[float]) -> None:
        self.mount = parse_calibration_matrix(mount_matrix)
        if self.mount is None:
            raise ValueError('an explicit verified mount rotation is required')
        self.info = SensorInfo(backend='windows-winrt')
        self.latest = SensorReading(source='windows-winrt')
        self.anchor = None
        self.last_timestamp = None
        self.last_received = None

    def accept(self, frame: dict, *, received_at: float, wall_time: float) -> SensorReading:
        try:
            value = parse_sample(frame)
        except SensorProbeError as error:
            raise SensorError(str(error)) from error
        device = value['device']
        if frame.get('candidates') != [device]:
            raise SensorError('accelerometer selection is ambiguous')
        if self.info.device and self.info.device != device:
            raise SensorError('accelerometer identity changed')
        stamp, observed = _timestamp(value['timestamp']), _timestamp(frame.get('observed_at'))
        if any(type(number) not in (int, float) or not math.isfinite(number)
               for number in (received_at, wall_time)):
            raise SensorError('invalid acquisition clock')
        if self.last_received is not None and received_at < self.last_received:
            raise SensorError('acquisition time moved backwards')
        # Check both the child observation and receipt age. Pipe backlog and
        # a sensor returning an old cached frame must fail before publication.
        if not -.05 <= observed - stamp <= SAMPLE_MAX_AGE or not -.05 <= wall_time - observed <= SAMPLE_MAX_AGE:
            raise StaleSample('sensor sample is old or from the future')
        if self.anchor is None:
            self.anchor = received_at, wall_time
        anchor_mono, anchor_wall = self.anchor
        if abs((wall_time - anchor_wall) - (received_at - anchor_mono)) > CLOCK_TOLERANCE:
            raise SensorError('wall clock changed during acquisition')
        mapped = anchor_mono + stamp - anchor_wall
        if not -.05 <= received_at - mapped <= SAMPLE_MAX_AGE:
            raise StaleSample('sensor sample is stale')
        self.last_received = received_at
        if self.last_timestamp is not None:
            if stamp < self.last_timestamp:
                raise SensorError('sensor time moved backwards')
            if stamp == self.last_timestamp:
                return self.latest
        self.latest = SensorReading(apply_calibration(value['acceleration_m_s2'], self.mount),
                                    min(received_at, mapped), 'windows-winrt')
        self.last_timestamp = stamp
        self.info = SensorInfo(ready=True, backend='windows-winrt', device=device)
        return self.latest


def _parent_start_time() -> int:
    import ctypes as C
    kernel = C.WinDLL('kernel32', use_last_error=True, winmode=0x800)
    kernel.GetProcessTimes.argtypes = [C.c_void_p, *([C.POINTER(C.c_uint64)] * 4)]
    kernel.GetProcessTimes.restype = C.c_int32
    times = [C.c_uint64() for _ in range(4)]
    if not kernel.GetProcessTimes(C.c_void_p(-1), *(C.byref(value) for value in times)):
        raise SensorError('cannot identify the sensor parent process')
    return times[0].value


class PowerShellSource:
    """Bounded stream from a fixed system executable; cancellation kills it."""

    def __init__(self, *, include_orientation: bool = False) -> None:
        if sys.platform != 'win32':
            raise SensorError('run WinRT acquisition inside Windows')
        # Pin the actual parent process instance. Even a forced backend exit
        # must stop the read-only WinRT helper; a reused PID is not that parent.
        if type(include_orientation) is not bool:
            raise ValueError('orientation acquisition must be explicitly enabled')
        program = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        script = STREAM_SCRIPT.replace('__PARENT_PID__', str(os.getpid())).replace('__PARENT_START__', str(_parent_start_time()))
        script = script.replace('__ORIENTATION_SETUP__', ORIENTATION_SETUP if include_orientation else '')
        script = script.replace('__ORIENTATION_READ__', ORIENTATION_READ if include_orientation else '')
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        self.frames = queue.Queue(maxsize=32)
        self.stopped = threading.Event()
        self.process = subprocess.Popen([str(program), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.reader = threading.Thread(target=self._read, daemon=True, name='winrt-pipe')
        self.reader.start()

    def _enqueue(self, value):
        while not self.stopped.is_set():
            try:
                self.frames.put(value, timeout=.1)
                return
            except queue.Full:
                continue

    def _read(self):
        try:
            while not self.stopped.is_set():
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line:
                    raise SensorError('WinRT stream disconnected')
                received, wall = time.monotonic(), time.time()
                self._enqueue((decode(line), received, wall))
        except (OSError, ValueError) as error:
            self._enqueue(SensorError(str(error)))

    def read(self, *, timeout: float) -> tuple[dict, float, float]:
        value = self.frames.get(timeout=timeout)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self) -> None:
        self.stopped.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.reader.join(timeout=1)
        if self.reader.is_alive():
            raise SensorError('WinRT pipe reader did not stop')
        self.process.stdout.close()


class WindowsSensor:
    """Threaded, read-only SensorBackend with bounded stale/startup retries."""

    def __init__(self, *, mount_matrix: Sequence[float], source_factory: Callable | None = None,
                 clock: Callable[[], float] = time.monotonic, include_orientation: bool = False) -> None:
        self.mount_matrix = parse_calibration_matrix(mount_matrix)
        self.tracker = SampleTracker(mount_matrix=self.mount_matrix)
        if type(include_orientation) is not bool:
            raise ValueError('orientation acquisition must be explicitly enabled')
        self.clock = clock
        self.source_factory = source_factory if source_factory is not None else (
            lambda: PowerShellSource(include_orientation=include_orientation))
        self._include_orientation = include_orientation
        self._orientation = OrientationTracker()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._error = 'starting'
        self._cleanup_error = None

    def start(self) -> None:
        if self._thread is not None or self._stop.is_set():
            raise RuntimeError('sensor already started or closed')
        self._thread = threading.Thread(target=self._run, daemon=True, name='winrt-sensor')
        self._thread.start()

    def _publish(self, frame: dict, received: float, wall: float) -> None:
        with self._lock:
            previous = self.tracker.latest.stamp
            self.tracker.accept(frame, received_at=received, wall_time=wall)
            self._error = None
            if self._include_orientation and self.tracker.latest.stamp != previous:
                try:
                    value = frame.get('orientation')
                    if not isinstance(value, dict):
                        raise OrientationError('orientation-sensor-unavailable')
                    observed = _timestamp(value.get('observed_at'))
                    if (not -.05 <= _timestamp(frame.get('observed_at')) - observed <= SAMPLE_MAX_AGE or
                            not -.05 <= wall - observed <= SAMPLE_MAX_AGE):
                        raise OrientationError('orientation-observation-is-stale')
                    anchor_mono, anchor_wall = self.tracker.anchor
                    mapped = anchor_mono + observed - anchor_wall
                    if not -.05 <= received - mapped <= SAMPLE_MAX_AGE:
                        raise OrientationError('orientation-observation-clock-invalid')
                    self._orientation.accept(value, stamp=min(received, mapped))
                except (OrientationError, SensorError) as error:
                    # Direction is optional; fixed modes use the valid gravity
                    # frame. Never count its earlier measurement as the later
                    # orientation snapshot time for dwell after slow WinRT work.
                    self._orientation.reset(str(error))

    def _run(self) -> None:
        while not self._stop.is_set():
            source = None
            try:
                with self._lock:
                    previous = self.tracker
                    self.tracker = SampleTracker(mount_matrix=self.mount_matrix)
                    # A retry re-anchors the clocks after suspend/time changes,
                    # but cannot bless a replacement device or count the same
                    # cached reading again. Mount validation belongs to the
                    # selected device for the entire manager lifetime.
                    self.tracker.info = replace(previous.info, ready=False)
                    self.tracker.last_timestamp = previous.last_timestamp
                    self._orientation.reset()
                source = self.source_factory()
                started = self.clock()
                while not self._stop.is_set():
                    try:
                        frame, received, wall = source.read(timeout=.1)
                        try:
                            self._publish(frame, received, wall)
                        except StaleSample as error:
                            # WinRT can return its pre-open cached frame first.
                            # Invalidate it, then wait within the existing
                            # startup/stale deadline for a genuinely new one.
                            with self._lock:
                                self._error = str(error)
                    except queue.Empty:
                        pass
                    with self._lock:
                        latest = self.tracker.latest.stamp
                    if self.clock() - (latest if latest is not None else started) > (
                            SAMPLE_MAX_AGE if latest is not None else STARTUP_TIMEOUT):
                        raise SensorError('sensor stream stopped producing fresh samples')
            except (OSError, ValueError) as error:
                with self._lock:
                    self._error = str(error)[:200]
            finally:
                if source is not None:
                    try:
                        source.close()
                    except (OSError, ValueError) as error:
                        with self._lock:
                            self._error = 'could not close the sensor source'
                            self._cleanup_error = error
            if self._cleanup_error is not None:
                return
            self._stop.wait(RETRY_SECONDS)

    @property
    def info(self) -> SensorInfo:
        with self._lock:
            info, stamp = self.tracker.info, self.tracker.latest.stamp
            error = self._error
            if self._stop.is_set():
                error = 'closed'
            elif stamp is None or not 0 <= self.clock() - stamp <= SAMPLE_MAX_AGE:
                error = error or 'sensor sample is stale'
            return replace(info, ready=error is None, error=error)

    def reading(self) -> SensorReading:
        with self._lock:
            value = self.tracker.latest
            if self._error is not None or self._stop.is_set() or value.stamp is None or not 0 <= self.clock() - value.stamp <= SAMPLE_MAX_AGE:
                return SensorReading(source='windows-winrt')
            return value

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # The worker polls its private queue every 100ms, then owns all
            # process cleanup. No concurrent close of a blocked pipe reader.
            thread.join(timeout=4)
            if thread.is_alive():
                raise SensorError('sensor worker did not stop')
        if self._cleanup_error is not None:
            raise SensorError('could not close the sensor source') from self._cleanup_error

    def _orientation_valid(self, now: float) -> bool:
        value = self._orientation.latest
        return bool(self._include_orientation and self._error is None and not self._stop.is_set() and
                    value is not None and 0 <= now - value.stamp <= SAMPLE_MAX_AGE)

    def orientation(self) -> OrientationReading | None:
        with self._lock:
            return self._orientation.latest if self._orientation_valid(self.clock()) else None

    @property
    def orientation_info(self) -> SensorInfo:
        with self._lock:
            info = self._orientation.info
            if self._orientation_valid(self.clock()):
                return info
            error = ('orientation-not-enabled' if not self._include_orientation else
                     'closed' if self._stop.is_set() else self._error or info.error or 'orientation-data-stale')
            return replace(info, ready=False, error=error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=10, choices=range(1, 61), metavar='1..60')
    parser.add_argument('--orientation', action='store_true', help='also observe the coarse system orientation without writing it')
    options = parser.parse_args(argv)
    if sys.platform != 'win32':
        parser.error('run this read-only monitor inside Windows')
    sensor = WindowsSensor(mount_matrix=IDENTITY, include_orientation=options.orientation)
    sensor.start()
    deadline = time.monotonic() + STARTUP_TIMEOUT
    count, last = 0, None
    try:
        while time.monotonic() < deadline:
            reading = sensor.reading()
            if reading.valid and reading.stamp != last:
                if count == 0:
                    deadline = time.monotonic() + options.seconds
                value = dict(version=1, source='windows-winrt', device=sensor.info.device,
                    mount_verified=False, stamp=reading.stamp, acceleration_m_s2=reading.vector,
                    magnitude_m_s2=reading.magnitude)
                if options.orientation:
                    direction = sensor.orientation()
                    value.update(orientation=asdict(direction) if direction else None,
                                 orientation_info=asdict(sensor.orientation_info))
                print(json.dumps(value, allow_nan=False), flush=True)
                count, last = count + 1, reading.stamp
            elif count and not sensor.info.ready:
                raise SensorError(sensor.info.error or 'sensor unavailable')
            time.sleep(.05)
        if not count:
            raise SensorError(sensor.info.error or 'sensor startup timed out')
    except (SensorError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    finally:
        sensor.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
