"""Windows acquisition boundaries: real timestamps, fake clocks and processes."""
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from panasonic_posture.windows.sensor import SampleTracker, SensorError, PowerShellSource, WindowsSensor

IDENTITY = (1., 0., 0., 0., 1., 0., 0., 0., 1.)


def frame(stamp=1000., **changes):
    value = dict(version=1, device='sensor-1', minimum_report_interval_ms=10,
                 timestamp=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
                 observed_at=datetime.fromtimestamp(stamp + .02, timezone.utc).isoformat(),
                 candidates=['sensor-1'], x=0., y=0., z=-1.)
    value.update(changes)
    return value


class SampleTrackerTests(unittest.TestCase):
    def tracker(self, matrix=IDENTITY):
        return SampleTracker(mount_matrix=matrix)

    def test_maps_sample_age_to_engine_clock_and_mount_coordinates(self):
        tracker = self.tracker((0., -1., 0., 1., 0., 0., 0., 0., 1.))
        reading = tracker.accept(frame(x=1., z=0.), received_at=50., wall_time=1000.1)
        self.assertAlmostEqual(reading.stamp, 49.9)
        self.assertAlmostEqual(reading.vector[1], 9.80665)
        self.assertAlmostEqual(reading.vector[0], 0.)
        self.assertEqual(tracker.info.device, 'sensor-1')

    def test_duplicate_samples_never_renew_freshness(self):
        tracker = self.tracker()
        first = tracker.accept(frame(), received_at=50., wall_time=1000.1)
        second = tracker.accept(frame(observed_at=datetime.fromtimestamp(1000.52, timezone.utc).isoformat()),
                                received_at=50.5, wall_time=1000.6)
        self.assertEqual(first, second)
        with self.assertRaises(SensorError):
            tracker.accept(frame(), received_at=53., wall_time=1003.1)

    def test_rejects_future_old_reordered_and_clock_jump_samples(self):
        for bad, now, wall in ((frame(1001.), 50., 1000.1), (frame(990.), 50., 1000.1),
                               (frame(999.9), 50.1, 1000.2), (frame(1000.2), 50.2, 1100.3)):
            tracker = self.tracker()
            tracker.accept(frame(), received_at=50., wall_time=1000.1)
            with self.subTest(bad=bad), self.assertRaises(SensorError):
                tracker.accept(bad, received_at=now, wall_time=wall)

    def test_refuses_ambiguous_changed_and_forged_identity(self):
        for changes in ({'candidates': []}, {'candidates': ['sensor-1', 'sensor-2']},
                        {'candidates': ['sensor-2']}, {'candidates': ['sensor-1', 'sensor-1']},
                        {'candidates': 'sensor-1'}, {'device': 'sensor-2', 'candidates': ['sensor-2']}):
            tracker = self.tracker()
            tracker.accept(frame(), received_at=50., wall_time=1000.1)
            with self.subTest(changes=changes), self.assertRaises(SensorError):
                tracker.accept(frame(1000.1, **changes), received_at=50.1, wall_time=1000.2)

    def test_requires_explicit_proper_mount_and_strict_timestamps(self):
        for matrix in (None, [1.] * 9, (-1., 0., 0., 0., 1., 0., 0., 0., 1.)):
            with self.subTest(matrix=matrix), self.assertRaises(ValueError):
                self.tracker(matrix)
        for changes in ({'timestamp': '2026-11-08'}, {'observed_at': 'bad'},
                        {'observed_at': datetime.fromtimestamp(999., timezone.utc).isoformat()},
                        {'x': True}, {'x': float('nan')}):
            with self.subTest(changes=changes), self.assertRaises(SensorError):
                self.tracker().accept(frame(**changes), received_at=50., wall_time=1000.1)


class SourceTests(unittest.TestCase):
    def test_non_windows_never_spawns_and_stream_script_never_writes_sensor_settings(self):
        with patch('panasonic_posture.windows.sensor.sys.platform', 'linux'), \
             patch('panasonic_posture.windows.sensor.subprocess.Popen') as spawn:
            with self.assertRaises(SensorError):
                PowerShellSource()
        spawn.assert_not_called()
        from panasonic_posture.windows.sensor import STREAM_SCRIPT
        self.assertIn('FindAllAsync', STREAM_SCRIPT)
        self.assertNotIn('.ReportInterval=', STREAM_SCRIPT)
        self.assertNotIn('.ReadingTransform=', STREAM_SCRIPT)

    def test_optional_direction_script_has_no_sensor_setting_writes_and_never_uses_a_shell(self):
        import base64
        process = SimpleNamespace(stdout=io.BytesIO(b''), poll=lambda: 0, wait=lambda timeout: 0)
        for enabled in (False, True):
            with patch('panasonic_posture.windows.sensor.sys.platform', 'win32'), \
                 patch('panasonic_posture.windows.sensor._parent_start_time', return_value=123), \
                 patch('panasonic_posture.windows.sensor.subprocess.Popen', return_value=process) as spawn:
                process.stdout = io.BytesIO(b'')
                source = PowerShellSource(include_orientation=enabled)
                source.close()
                arguments = spawn.call_args.args[0]
                script = base64.b64decode(arguments[-1]).decode('utf-16-le')
                self.assertEqual('SimpleOrientationSensor' in script, enabled)
                self.assertNotIn('__ORIENTATION_', script)
                self.assertNotRegex(script, r'\.(ReportInterval|ReadingTransform)\s*=')
                self.assertNotIn('shell', spawn.call_args.kwargs)

    def test_process_frames_are_bounded_and_eof_is_a_failure(self):
        payload = json.dumps(frame()).encode() + b'\n'
        process = SimpleNamespace(stdout=io.BytesIO(payload), pid=123,
            poll=lambda: 0, wait=lambda timeout: 0)
        with patch('panasonic_posture.windows.sensor.sys.platform', 'win32'), \
             patch('panasonic_posture.windows.sensor._parent_start_time', return_value=123), \
             patch('panasonic_posture.windows.sensor.subprocess.Popen', return_value=process) as spawn:
            source = PowerShellSource()
            try:
                value, _, _ = source.read(timeout=2)
                self.assertEqual(value['device'], 'sensor-1')
                with self.assertRaises(SensorError):
                    source.read(timeout=2)
            finally:
                source.close()
        self.assertNotIn('shell', spawn.call_args.kwargs)
        self.assertTrue(Path(spawn.call_args.args[0][0]).name.endswith('powershell.exe'))

    def test_oversized_or_invalid_frame_is_rejected(self):
        for payload in (b'x' * 65537, b'{"version":1,"version":1}\n'):
            process = SimpleNamespace(stdout=io.BytesIO(payload), poll=lambda: 0, wait=lambda timeout: 0)
            with patch('panasonic_posture.windows.sensor.sys.platform', 'win32'), \
                 patch('panasonic_posture.windows.sensor._parent_start_time', return_value=123), \
                 patch('panasonic_posture.windows.sensor.subprocess.Popen', return_value=process):
                source = PowerShellSource()
                try:
                    with self.assertRaises(SensorError):
                        source.read(timeout=2)
                finally:
                    source.close()


class ManagerTests(unittest.TestCase):
    def until(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(.01)
        self.fail('sensor worker did not reach the expected state')

    def test_failure_retry_pins_identity_and_close_releases_the_source(self):
        class Source:
            def __init__(self):
                self.frames = queue.Queue()
                self.closed = threading.Event()

            def read(self, *, timeout):
                item = self.frames.get(timeout=timeout)
                if isinstance(item, Exception):
                    raise item
                return item

            def publish(self, **changes):
                wall, mono = time.time(), time.monotonic()
                self.frames.put((frame(wall - .02, **changes), mono, wall))

            def close(self):
                self.closed.set()

        sources = []

        def factory():
            source = Source()
            sources.append(source)
            return source

        sensor = WindowsSensor(mount_matrix=IDENTITY, source_factory=factory)
        self.addCleanup(sensor.close)
        sensor.start()
        self.until(lambda: len(sources) == 1)
        wall, mono = time.time(), time.monotonic()
        sources[0].frames.put((frame(wall - 60., observed_at=datetime.fromtimestamp(
            wall, timezone.utc).isoformat()), mono, wall))
        self.until(lambda: sensor.info.error == 'sensor sample is old or from the future')
        self.assertFalse(sensor.reading().valid)
        self.assertEqual(len(sources), 1)
        self.assertFalse(sources[0].closed.is_set())
        sources[0].publish()
        self.until(lambda: sensor.info.ready)
        sources[0].frames.put(SensorError('injected disconnect'))
        self.until(lambda: sensor.info.error == 'injected disconnect')
        self.assertFalse(sensor.reading().valid)
        self.assertTrue(sources[0].closed.wait(2))
        self.until(lambda: len(sources) == 2)
        sources[1].publish(device='replacement', candidates=['replacement'])
        self.until(lambda: sensor.info.error == 'accelerometer identity changed')
        self.assertFalse(sensor.reading().valid)
        self.until(lambda: len(sources) == 3)
        sources[2].publish()
        self.until(lambda: sensor.info.ready)
        sensor.close()
        self.assertTrue(sources[2].closed.is_set())
        self.assertFalse(sensor.info.ready)
        self.assertFalse(sensor.reading().valid)

    def test_does_not_spawn_before_start_and_fails_closed_after_stale_or_close(self):
        now = [50.]
        manager = WindowsSensor(mount_matrix=IDENTITY, clock=lambda: now[0])
        self.assertFalse(manager.info.ready)
        self.assertFalse(manager.reading().valid)
        # Publish using the same acquisition path without opening a device.
        manager._publish(frame(), 50., 1000.1)
        self.assertTrue(manager.info.ready)
        self.assertTrue(manager.reading().valid)
        now[0] = 53.
        self.assertFalse(manager.info.ready)
        self.assertFalse(manager.reading().valid)
        manager.close()
        self.assertFalse(manager.info.ready)
        self.assertFalse(manager.reading().valid)


if __name__ == '__main__':
    unittest.main()
