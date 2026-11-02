"""Tests for :mod:`panasonic_posture.service.sensor` with fake data sources."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from panasonic_posture.model import SensorSource
from panasonic_posture.service import sensor as sensor_module
from panasonic_posture.service import sensor_sources
from panasonic_posture.service.sensor import (
    IioSampleSource,
    SensorManager,
    SourceUnavailable,
    SysfsSampleSource,
)

PROFILE = SimpleNamespace(
    accel_name="accel_3d",
    mount_matrix=None,
    switch_name=None,
)
VECTOR = (0.0, 0.0, -9.81)


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class AutoClock:
    """A monotonic clock that advances a little on every reading."""

    def __init__(self, start: float = 100.0, step: float = 0.01) -> None:
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class FakeSourceFactory:
    """Factory for :class:`FakeSource` with scripted behaviour."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.opens = 0
        self.closes = 0
        self.reads = 0
        self.fail_open = False
        self.repeat_last = True
        self.empty_wait = 0.005
        self.script: deque[tuple] = deque()
        self.stop = threading.Event()
        self.last: tuple[float, float, float] | None = None

    def __call__(self):
        return FakeSource(self)

    def vector(self, value=VECTOR):
        self.script.append(("vector", value))
        return self

    def none(self):
        self.script.append(("none",))
        return self

    def failure(self, message: str = "synthetic failure"):
        self.script.append(("raise", message))
        return self


class FakeSource:
    kind = ""

    def __init__(self, factory: FakeSourceFactory) -> None:
        self.kind = factory.kind
        self._factory = factory

    def open(self) -> None:
        self._factory.opens += 1
        if self._factory.fail_open:
            raise SourceUnavailable(f"{self.kind}: cannot open")

    def read(self):
        factory = self._factory
        factory.reads += 1
        if factory.script:
            item = factory.script.popleft()
            if item[0] == "vector":
                factory.last = item[1]
                return item[1]
            if item[0] == "raise":
                raise SourceUnavailable(item[1])
            return None
        if factory.repeat_last and factory.last is not None:
            return factory.last
        factory.stop.wait(factory.empty_wait)
        return None

    def close(self) -> None:
        self._factory.closes += 1


class SensorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.stop = threading.Event()
        self.clock = AutoClock()
        self.boot_clock = lambda: 1234.5
        self.iio = FakeSourceFactory(str(SensorSource.IIO))
        self.sysfs = FakeSourceFactory(str(SensorSource.SYSFS))
        self.iio.stop = self.stop
        self.sysfs.stop = self.stop
        self.managers: list[SensorManager] = []

    def tearDown(self) -> None:
        for manager in self.managers:
            manager.close()

    def make(self, *, managed: bool = True, clock: AutoClock | None = None,
             monotonic=None, boot_clock=None) -> SensorManager:
        manager = SensorManager(
            self.stop,
            PROFILE,
            managed,
            monotonic=monotonic or clock or self.clock,
            clock=boot_clock or self.boot_clock,
            iio_factory=self.iio,
            sysfs_factory=self.sysfs,
        )
        self.managers.append(manager)
        return manager


class SourceSelectionTests(SensorTestCase):
    def test_prefers_iio_source(self) -> None:
        self.iio.vector((0.1, 0.2, -9.8))
        self.sysfs.fail_open = True
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: manager.reading().vector is not None))
        self.assertEqual(manager.source, SensorSource.IIO)
        self.assertGreaterEqual(self.iio.opens, 1)
        self.assertIsNone(manager.sensor_error)
        reading = manager.reading()
        self.assertEqual(reading.source, SensorSource.IIO)
        self.assertEqual(reading.stamp, 1234.5)
        self.assertEqual(reading.vector, (0.1, 0.2, -9.8))

    def test_falls_back_to_sysfs_when_iio_cannot_open(self) -> None:
        self.iio.fail_open = True
        self.sysfs.vector()
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        self.assertTrue(wait_for(lambda: manager.reading().vector == VECTOR))
        self.assertGreaterEqual(self.iio.opens, 1)
        self.assertEqual(manager.reading().source, SensorSource.SYSFS)
        self.assertIsNone(manager.sensor_error)

    def test_iio_without_samples_falls_back_to_sysfs(self) -> None:
        self.iio.repeat_last = False  # opens fine but never produces a sample
        self.sysfs.vector()
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        self.assertTrue(wait_for(lambda: manager.reading().vector == VECTOR))
        self.assertGreaterEqual(self.iio.opens, 1)
        self.assertGreaterEqual(self.iio.closes, 1)

    def test_iio_shadow_trial_recovers_after_retry_interval(self) -> None:
        self.iio.fail_open = True
        self.iio.vector()
        self.sysfs.vector()
        clock = AutoClock(step=0.01)
        with (
            mock.patch.object(sensor_module, "SENSOR_RETRY_SECONDS", 0.2),
            mock.patch.object(sensor_module, "IIO_TRIAL_SECONDS", 0.2),
        ):
            manager = self.make(clock=clock)
            manager.start()
            self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
            first_attempts = self.iio.opens
            self.assertTrue(
                wait_for(lambda: self.iio.opens > first_attempts),
                "IIO shadow trial was not retried after the retry interval",
            )
            self.assertEqual(manager.source, SensorSource.SYSFS)
            self.iio.fail_open = False
            self.assertTrue(wait_for(lambda: manager.source == SensorSource.IIO))
            self.assertTrue(
                wait_for(lambda: manager.reading().source == SensorSource.IIO)
            )

    def test_prefer_shared_uses_sysfs_and_switches_back_to_iio(self) -> None:
        self.sysfs.vector()
        self.iio.vector((1.0, 0.0, -9.8))
        manager = self.make()
        manager.set_prefer_shared(True)
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        self.assertEqual(self.iio.opens, 0)
        manager.set_prefer_shared(False)
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.IIO))
        self.assertGreaterEqual(self.iio.opens, 1)
        self.assertTrue(wait_for(lambda: manager.reading().vector == (1.0, 0.0, -9.8)))

    def test_both_sources_failing_is_reported(self) -> None:
        self.iio.fail_open = True
        self.sysfs.fail_open = True
        manager = self.make()
        manager.start()
        self.assertIsNone(manager.reading().vector)
        self.assertTrue(wait_for(lambda: manager.sensor_error is not None))
        # The error text is whichever source failed last; both must be tried.
        self.assertTrue(
            wait_for(lambda: "sysfs:" in (manager.sensor_error or "")),
            manager.sensor_error,
        )
        self.assertEqual(manager.source, SensorSource.STARTING)


class SensorLifecycleTests(SensorTestCase):
    def test_managed_false_does_not_acquire(self) -> None:
        manager = self.make(managed=False)
        manager.start()
        time.sleep(0.05)
        self.assertEqual(self.iio.opens, 0)
        self.assertEqual(self.sysfs.opens, 0)
        self.assertEqual(manager.source, SensorSource.STARTING)
        self.assertIsNone(manager.reading().vector)

    def test_close_stops_worker(self) -> None:
        self.iio.vector()
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: manager.reading().vector is not None))
        thread = manager._thread
        self.assertIsNotNone(thread)
        manager.close()
        self.assertIsNone(manager._thread)
        self.assertFalse(thread.is_alive())

    def test_empty_reads_are_cadenced_instead_of_hot_looping(self) -> None:
        self.iio.repeat_last = False
        self.iio.empty_wait = 0.0  # an immediate empty result is the worst case
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: self.iio.opens >= 1))
        before = self.iio.reads
        time.sleep(0.3)
        reads = self.iio.reads - before
        # At the 20 Hz cadence this is ~6 reads; without throttling an
        # immediate None result would consume the CPU and produce thousands.
        self.assertLessEqual(reads, 10)
        self.assertGreaterEqual(reads, 3)

    def test_close_is_bounded_while_source_read_blocks(self) -> None:
        release = threading.Event()

        class BlockingSource:
            kind = str(SensorSource.IIO)

            def open(self) -> None:
                pass

            def read(self):
                release.wait(5.0)

            def close(self) -> None:
                pass

        manager = SensorManager(
            self.stop,
            PROFILE,
            managed=True,
            monotonic=time.monotonic,
            clock=self.boot_clock,
            iio_factory=BlockingSource,
            sysfs_factory=self.sysfs,
        )
        self.managers.append(manager)
        manager.start()
        self.assertTrue(wait_for(lambda: manager._thread is not None))
        with mock.patch.object(sensor_module, "SENSOR_CLOSE_TIMEOUT_SECONDS", 0.1):
            started = time.monotonic()
            manager.close()
            elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 0.5)

    def test_external_stop_event_ends_worker(self) -> None:
        self.iio.vector()
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: manager.reading().vector is not None))
        thread = manager._thread
        self.stop.set()
        self.assertTrue(wait_for(lambda: not thread.is_alive()))
        # Draining the error state is not required; the thread must just stop.
        manager.close()

    def test_prefer_shared_property(self) -> None:
        manager = self.make()
        self.assertFalse(manager.prefer_shared)
        manager.set_prefer_shared(True)
        self.assertTrue(manager.prefer_shared)
        manager.set_prefer_shared(True)  # idempotent
        self.assertTrue(manager.prefer_shared)


class SensorExportTests(unittest.TestCase):
    def test_split_source_types_remain_public_from_sensor_module(self) -> None:
        for name in (
            "DEFAULT_ACCEL_NAME",
            "IIO_DEVICE_GLOB",
            "IIO_NO_SAMPLE_SECONDS",
            "IIO_TRIAL_SECONDS",
            "IioSampleSource",
            "SENSOR_CLOSE_TIMEOUT_SECONDS",
            "SENSOR_MIN_READ_INTERVAL_SECONDS",
            "SENSOR_RETRY_SECONDS",
            "SYSFS_RETRY_SECONDS",
            "SYSFS_SLOW_READ_SECONDS",
            "SampleSource",
            "SensorManager",
            "SourceUnavailable",
            "SysfsSampleSource",
        ):
            self.assertTrue(hasattr(sensor_module, name), name)
            self.assertIn(name, sensor_module.__all__, name)
        self.assertIs(sensor_module.IioSampleSource, sensor_sources.IioSampleSource)
        self.assertIs(sensor_module.SysfsSampleSource, sensor_sources.SysfsSampleSource)
        self.assertIs(sensor_module.SourceUnavailable, sensor_sources.SourceUnavailable)


class NoClaimTests(unittest.TestCase):
    def test_sensor_module_never_claims_the_proxy(self) -> None:
        source = Path(__file__).resolve().parents[1] / "src" / "panasonic_posture" / "service" / "sensor.py"
        text = source.read_text(encoding="utf-8")
        # The module may *document* the trade-off, but must never call the
        # iio-sensor-proxy claim API or talk to its bus name.
        self.assertNotIn("ClaimAccelerometer(", text)
        self.assertNotIn("ReleaseAccelerometer(", text)
        self.assertNotIn("net.hadess.SensorProxy", text)


class ShadowTrialTests(SensorTestCase):
    """The IIO retry must never stop the sysfs sample stream (review Q-04)."""

    def setUp(self) -> None:
        super().setUp()
        self._patches = [
            mock.patch.object(sensor_module, "SENSOR_RETRY_SECONDS", 0.05),
            mock.patch.object(sensor_module, "IIO_TRIAL_SECONDS", 0.1),
        ]
        for patcher in self._patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_silent_iio_trial_keeps_sysfs_stream_gap_free(self) -> None:
        # IIO accepts the buffer setup but never produces a sample (known
        # firmware behaviour); sysfs must keep feeding the engine meanwhile.
        self.iio.repeat_last = False
        self.sysfs.vector()
        manager = self.make(
            monotonic=time.monotonic, boot_clock=time.monotonic
        )
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        first_attempts = self.iio.opens
        self.assertTrue(
            wait_for(lambda: self.iio.opens > first_attempts),
            "shadow IIO trial never started",
        )
        deadline = time.monotonic() + 1.2
        last_stamp = None
        last_change = time.monotonic()
        max_gap = 0.0
        while time.monotonic() < deadline:
            reading = manager.reading()
            if reading.stamp is not None and reading.stamp != last_stamp:
                now = time.monotonic()
                if last_stamp is not None:
                    max_gap = max(max_gap, now - last_change)
                last_stamp = reading.stamp
                last_change = now
            time.sleep(0.002)
        self.assertEqual(manager.source, SensorSource.SYSFS)
        self.assertLess(max_gap, 0.3, f"sysfs stream had a {max_gap:.2f}s gap")
        self.assertIsNone(manager.sensor_error)
        self.assertGreaterEqual(self.iio.opens, 2)

    def test_single_sample_iio_trial_keeps_sysfs_stream_gap_free(self) -> None:
        # Regression: this firmware can emit one IIO sample during the shadow
        # trial and then go silent.  Treating that frame as a healthy handover
        # stopped sysfs for the full IIO no-sample timeout, which made the
        # policy release an otherwise stationary tent takeover.
        self.iio.fail_open = True
        self.sysfs.vector()
        manager = self.make(
            monotonic=time.monotonic, boot_clock=time.monotonic
        )
        with mock.patch.object(sensor_module, "IIO_NO_SAMPLE_SECONDS", 0.2):
            manager.start()
            self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
            self.iio.fail_open = False
            self.iio.repeat_last = False
            self.iio.vector((1.0, 2.0, -9.8))
            manager._iio_retry_at = 0.0
            attempts = self.iio.opens
            self.assertTrue(
                wait_for(lambda: self.iio.opens > attempts),
                "shadow IIO trial never started",
            )

            deadline = time.monotonic() + 0.35
            last_stamp = None
            last_change = time.monotonic()
            max_gap = 0.0
            sources = set()
            while time.monotonic() < deadline:
                reading = manager.reading()
                sources.add(reading.source)
                if reading.stamp is not None and reading.stamp != last_stamp:
                    now = time.monotonic()
                    if last_stamp is not None:
                        max_gap = max(max_gap, now - last_change)
                    last_stamp = reading.stamp
                    last_change = now
                time.sleep(0.002)

        self.assertNotIn(SensorSource.IIO, sources)
        self.assertEqual(manager.source, SensorSource.SYSFS)
        self.assertLess(max_gap, 0.15, f"sysfs stream had a {max_gap:.2f}s gap")
        self.assertIsNone(manager.sensor_error)

    def test_failed_shadow_trial_does_not_report_sensor_error(self) -> None:
        self.iio.repeat_last = False
        self.sysfs.vector()
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        first_attempts = self.iio.opens
        self.assertTrue(wait_for(lambda: self.iio.opens > first_attempts))
        time.sleep(0.05)
        self.assertEqual(manager.source, SensorSource.SYSFS)
        self.assertIsNone(manager.sensor_error)
        self.assertTrue(wait_for(lambda: manager.reading().vector == VECTOR))

    def test_shadow_trial_switches_only_after_a_healthy_sample_window(self) -> None:
        self.iio.fail_open = True
        self.iio.repeat_last = False
        self.sysfs.vector()
        manager = self.make()
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        # The next trial sees several empty reads before the first real sample;
        # it must not become the active source before that sample arrives.
        for _ in range(3):
            self.iio.none()
        self.iio.vector((1.0, 2.0, -3.0))
        self.iio.repeat_last = True
        self.iio.fail_open = False
        first_attempts = self.iio.opens
        self.assertTrue(wait_for(lambda: self.iio.opens > first_attempts))
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.IIO))
        self.assertTrue(
            wait_for(lambda: manager.reading().vector == (1.0, 2.0, -3.0))
        )
        self.assertEqual(manager.reading().source, SensorSource.IIO)
        self.assertIsNone(manager.sensor_error)

    def test_prefer_shared_does_not_probe_iio(self) -> None:
        self.sysfs.vector()
        self.iio.vector()
        manager = self.make()
        manager.set_prefer_shared(True)
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        time.sleep(0.3)  # longer than the patched retry interval
        self.assertEqual(self.iio.opens, 0)

    def test_completed_trial_is_retained_until_handover_is_consumed(self) -> None:
        # Regression: the trial thread used to clear _trial in its finally
        # block.  If it finished between two sysfs polls, the manager lost the
        # open IIO source and immediately started another trial, cycling the
        # buffer and making sensor_source flap between IIO and sysfs.
        self.iio.vector((1.0, 2.0, -9.8))
        manager = self.make()
        manager._iio_retry_at = 0.0
        with mock.patch.object(sensor_module, "IIO_TRIAL_SECONDS", 0.05):
            manager._start_iio_trial()
            trial = manager._trial
            self.assertIsNotNone(trial)
            self.assertTrue(trial.done.wait(1.0))
        self.assertIs(manager._trial, trial)

        source = manager._poll_shadow_trial()
        self.assertIsNotNone(source)
        self.assertIsNone(manager._trial)
        self.assertEqual(manager.source, SensorSource.IIO)
        self.assertEqual(self.iio.opens, 1)
        self.assertEqual(self.iio.closes, 0)
        source.close()
        self.assertEqual(self.iio.closes, 1)

    def test_failed_trial_keeps_retry_backoff_until_consumed(self) -> None:
        # Regression: losing the failed trial also discarded its retry
        # deadline, so sysfs retried (and toggled) IIO almost continuously.
        self.iio.fail_open = True
        manager = self.make(monotonic=time.monotonic)
        manager._iio_retry_at = 0.0
        manager._start_iio_trial()
        trial = manager._trial
        self.assertIsNotNone(trial)
        self.assertTrue(trial.done.wait(1.0))
        self.assertIs(manager._trial, trial)

        self.assertIsNone(manager._poll_shadow_trial())
        self.assertIsNone(manager._trial)
        self.assertGreater(manager._iio_retry_at, time.monotonic())
        self.assertEqual(self.iio.opens, 1)
        self.assertIsNone(manager._poll_shadow_trial())
        self.assertEqual(self.iio.opens, 1)

    def test_cancel_closes_an_unconsumed_handover(self) -> None:
        self.iio.vector()
        manager = self.make()
        manager._iio_retry_at = 0.0
        with mock.patch.object(sensor_module, "IIO_TRIAL_SECONDS", 0.05):
            manager._start_iio_trial()
            trial = manager._trial
            self.assertIsNotNone(trial)
            self.assertTrue(trial.done.wait(1.0))
            self.assertIsNotNone(trial.source)

        manager._cancel_trial()
        self.assertIsNone(manager._trial)
        self.assertIsNone(trial.source)
        self.assertEqual(self.iio.closes, 1)

    def test_cancel_closes_a_handover_before_done_is_signalled(self) -> None:
        # The trial thread hands over the source before setting done.  A
        # cancellation in that narrow window must still close the source.
        manager = self.make()
        source = self.iio()
        source.open()
        trial = sensor_module._IioTrial()
        trial.source = source
        manager._trial = trial

        manager._cancel_trial()
        self.assertIsNone(trial.source)
        self.assertEqual(self.iio.closes, 1)

    def test_successful_handover_stops_trial_churn(self) -> None:
        # Exercise the complete worker path that produced the real-machine
        # flapping: the first IIO attempt fails, sysfs runs, then a shadow trial
        # succeeds.  The handed-over source must become the only reader.
        self.iio.fail_open = True
        self.iio.vector((1.0, 2.0, -9.8))
        self.sysfs.vector()
        manager = self.make(monotonic=time.monotonic)
        manager.start()
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.SYSFS))
        self.iio.fail_open = False
        manager._iio_retry_at = 0.0
        self.assertTrue(wait_for(lambda: manager.source == SensorSource.IIO))
        opens = self.iio.opens
        closes = self.iio.closes

        time.sleep(0.3)
        self.assertEqual(manager.source, SensorSource.IIO)
        self.assertEqual(self.iio.opens, opens)
        self.assertEqual(self.iio.closes, closes)
        self.assertIsNone(manager.sensor_error)


class IioSampleSourceTests(unittest.TestCase):
    class FakeAccel:
        def __init__(self, samples: list[tuple[float, float, float]]) -> None:
            self.samples = samples
            self.entered = 0
            self.exited = 0

        def __enter__(self):
            self.entered += 1
            return self

        def __exit__(self, *_exc):
            self.exited += 1

        def __iter__(self):
            return iter(self.samples)

    def test_reads_samples_and_closes(self) -> None:
        accel = self.FakeAccel([(0.0, 0.0, -9.8), (1.0, 2.0, 3.0)])
        source = IioSampleSource(accel)
        source.open()
        self.assertEqual(source.read(), (0.0, 0.0, -9.8))
        self.assertEqual(source.read(), (1.0, 2.0, 3.0))
        source.close()
        self.assertEqual(accel.entered, 1)
        self.assertEqual(accel.exited, 1)

    def test_stream_end_is_unavailable(self) -> None:
        source = IioSampleSource(self.FakeAccel([]))
        source.open()
        with self.assertRaises(SourceUnavailable):
            source.read()
        source.close()

    def test_malformed_sample_is_unavailable(self) -> None:
        class MalformedAccel:
            def __iter__(self):
                return iter([(0.0, -9.8)])

        source = IioSampleSource(MalformedAccel())
        source.open()
        with self.assertRaises(SourceUnavailable):
            source.read()

    def test_setup_failure_is_unavailable(self) -> None:
        class BrokenAccel:
            def __enter__(self):
                raise OSError("buffer busy")

            def __exit__(self, *_exc):
                pass

            def __iter__(self):
                return iter(())

        with self.assertRaises(SourceUnavailable):
            IioSampleSource(BrokenAccel()).open()


class SysfsSampleSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.device = self.root / "iio:device0"
        self.device.mkdir()

    def make_device(
        self,
        *,
        name: str = "accel_3d",
        scale: str = "0.1",
        offset: str | None = None,
        raw: tuple[str, str, str] = ("1", "2", "-3"),
    ) -> None:
        (self.device / "name").write_text(name + "\n", encoding="utf-8")
        (self.device / "in_accel_scale").write_text(scale + "\n", encoding="utf-8")
        if offset is not None:
            (self.device / "in_accel_offset").write_text(offset + "\n", encoding="utf-8")
        for axis, value in zip("xyz", raw):
            (self.device / f"in_accel_{axis}_raw").write_text(value + "\n", encoding="utf-8")

    def _assert_vector(self, actual, expected) -> None:
        self.assertEqual(len(actual), 3)
        for got, want in zip(actual, expected):
            self.assertAlmostEqual(got, want, places=9)

    def source(self, **kwargs) -> SysfsSampleSource:
        kwargs.setdefault("apply_mount", lambda values, matrix: tuple(values))
        return SysfsSampleSource(None, root=self.root, **kwargs)

    def test_reads_and_scales_without_mount_matrix(self) -> None:
        self.make_device(offset="1")
        source = self.source()
        source.open()
        self._assert_vector(source.read(), (0.2, 0.3, -0.2))
        source.close()

    def test_mount_matrix_is_applied(self) -> None:
        self.make_device()
        source = SysfsSampleSource(
            None,
            root=self.root,
            apply_mount=lambda values, matrix: tuple(-value for value in values),
        )
        source.open()
        self._assert_vector(source.read(), (-0.1, -0.2, 0.3))

    def test_malformed_mount_result_is_unavailable(self) -> None:
        self.make_device()
        source = SysfsSampleSource(
            None,
            root=self.root,
            apply_mount=lambda values, matrix: (values[0], values[1]),
        )
        source.open()
        with self.assertRaises(SourceUnavailable):
            source.read()

    def test_device_mount_matrix_is_used_when_profile_has_none(self) -> None:
        self.make_device()
        (self.device / "in_accel_mount_matrix").write_text(
            "0,-1,0,1,0,0,0,0,1\n", encoding="utf-8"
        )
        source = SysfsSampleSource(None, root=self.root)
        source.open()
        self._assert_vector(source.read(), (-0.2, 0.1, -0.3))
        source.close()

    def test_profile_mount_matrix_overrides_device_matrix(self) -> None:
        self.make_device()
        (self.device / "in_accel_mount_matrix").write_text(
            "0,-1,0,1,0,0,0,0,1\n", encoding="utf-8"
        )
        profile = SimpleNamespace(
            accel_name="accel_3d",
            mount_matrix=(1, 0, 0, 0, 1, 0, 0, 0, 1),
        )
        source = SysfsSampleSource(profile, root=self.root)
        source.open()
        self._assert_vector(source.read(), (0.1, 0.2, -0.3))

    def test_invalid_device_mount_matrix_falls_back_to_identity(self) -> None:
        self.make_device()
        (self.device / "in_accel_mount_matrix").write_text("1 2 3\n", encoding="utf-8")
        source = SysfsSampleSource(None, root=self.root)
        source.open()
        self._assert_vector(source.read(), (0.1, 0.2, -0.3))

    def test_invalid_utf8_device_name_is_skipped(self) -> None:
        self.make_device()
        junk = self.root / "iio:device1"
        junk.mkdir()
        (junk / "name").write_bytes(b"\xff\xfe broken\n")
        source = self.source()
        source.open()
        self._assert_vector(source.read(), (0.1, 0.2, -0.3))

    def test_ambiguous_devices_are_unavailable(self) -> None:
        self.make_device()
        second = self.root / "iio:device1"
        second.mkdir()
        (second / "name").write_text("accel_3d\n", encoding="utf-8")
        with self.assertRaises(SourceUnavailable):
            self.source().open()

    def test_missing_device_is_unavailable(self) -> None:
        with self.assertRaises(SourceUnavailable):
            self.source().open()

    def test_invalid_scale_is_unavailable(self) -> None:
        self.make_device(scale="not-a-number")
        with self.assertRaises(SourceUnavailable):
            self.source().open()

    def test_zero_scale_is_unavailable(self) -> None:
        # A zero scale would otherwise publish an all-zero vector forever.
        self.make_device(scale="0")
        with self.assertRaises(SourceUnavailable):
            self.source().open()

    def test_sample_read_error_is_unavailable(self) -> None:
        self.make_device()
        source = self.source()
        source.open()
        (self.device / "in_accel_y_raw").unlink()
        with self.assertRaises(SourceUnavailable):
            source.read()

    def test_slow_sample_is_unavailable(self) -> None:
        self.make_device()
        timeline = iter((10.0, 10.1, 10.2, 12.0))
        source = self.source(monotonic=lambda: next(timeline))
        source.open()
        with self.assertRaises(SourceUnavailable):
            source.read()

    def test_slow_first_axis_stops_reading_remaining_axes(self) -> None:
        self.make_device()
        # Only two clock readings are supplied: the start and the check after
        # the first axis.  Reaching the second axis would exhaust the iterator
        # instead of raising the intended slow-acquisition error.
        timeline = iter((10.0, 12.0))
        source = self.source(monotonic=lambda: next(timeline))
        source.open()
        with self.assertRaisesRegex(SourceUnavailable, "slow sensor"):
            source.read()


if __name__ == "__main__":
    unittest.main()
