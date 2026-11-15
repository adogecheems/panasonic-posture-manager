"""Startup CSM reads are allowed only when no software lease is pending."""
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from panasonic_posture.windows.hardware.convertible_slate import SlateError
from panasonic_posture.windows.hardware.slate_bootstrap import (
    StartupPhysicalBaseline,
    AsyncSlateReader,
    pending_slate_leases,
    read_bootstrap_slate_state,
)
from panasonic_posture.model import SensorReading


class SlateBootstrapTests(unittest.TestCase):
    def test_slow_startup_read_is_single_and_never_blocks_the_caller(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        def read():
            calls.append(True)
            entered.set()
            release.wait(2)
            return False
        reader = AsyncSlateReader(read)
        try:
            self.assertIsNone(reader())
            self.assertTrue(entered.wait(1))
            for _ in range(20):
                self.assertIsNone(reader())
            self.assertEqual(calls, [True])
        finally:
            release.set()
            reader._task.result(timeout=2)
        self.assertIs(reader(), False)

    def test_oversized_user_journal_is_rejected_without_quarantine_or_device_access(self):
        with tempfile.TemporaryDirectory() as directory:
            users = Path(directory)
            journal = users / 'user/AppData/Local/PanasonicPostureManager/native-display-slate.json'
            journal.parent.mkdir(parents=True)
            payload = b' ' * 8192 + b'{"version":1,"original":false,"desired":true}'
            journal.write_bytes(payload)
            device = Mock()
            with self.assertRaises(SlateError):
                read_bootstrap_slate_state(users_root=users, device_factory=device)
            device.assert_not_called()
            self.assertEqual(journal.read_bytes(), payload)

    def test_resume_requires_new_samples_and_a_complete_stability_window(self):
        now, stamp = [10.], [9.9]
        sensor = Mock()
        sensor.reading.side_effect = lambda: SensorReading((0., -9.5, -2.), stamp[0], 'iio')
        baseline = StartupPhysicalBaseline(
            slate_reader=lambda: None, sensor=sensor, clock=lambda: now[0], minimum_stamp=10.,
        )
        self.assertIsNone(baseline())
        stamp[0] = now[0] = 10.1
        self.assertIsNone(baseline())
        now[0] = 10.7
        self.assertIsNone(baseline(), 'one repeated sample cannot establish stability')
        stamp[0] = now[0]
        self.assertIs(baseline(), False)

    def test_corrupt_recovery_record_is_only_read_during_privileged_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            users = Path(directory)
            journal = (users / 'fixture-user' / 'AppData' / 'Local'
                       / 'PanasonicPostureManager' / 'native-display-slate.json')
            journal.parent.mkdir(parents=True)
            journal.write_bytes(b'{')
            device = Mock()
            with self.assertRaises(SlateError):
                read_bootstrap_slate_state(users_root=users, device_factory=device)
            device.assert_not_called()
            self.assertTrue(journal.exists())
            self.assertEqual(journal.read_bytes(), b'{')
            self.assertEqual(list(journal.parent.iterdir()), [journal])

    def test_reads_the_raw_indicator_when_the_previous_run_is_clean(self):
        closed = []

        class Device:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                closed.append(True)

            def read(self):
                return True

        with tempfile.TemporaryDirectory() as directory:
            users = Path(directory)
            (users / 'fixture-user' / 'AppData' / 'Local').mkdir(parents=True)
            actual = read_bootstrap_slate_state(
                users_root=users,
                device_factory=Device,
            )
        self.assertTrue(actual)
        self.assertEqual(closed, [True])

    def test_single_pending_journal_requires_independent_geometry(self):
        with tempfile.TemporaryDirectory() as directory:
            users = Path(directory)
            journal = (
                users / 'fixture-user' / 'AppData' / 'Local'
                / 'PanasonicPostureManager' / 'native-display-slate.json'
            )
            journal.parent.mkdir(parents=True)
            journal.write_text(
                '{"version":1,"original":true,"desired":false}',
                encoding='utf-8',
            )
            self.assertEqual([lease.original for lease in pending_slate_leases(users)], [True])
            device = Mock()
            self.assertIsNone(read_bootstrap_slate_state(
                users_root=users,
                device_factory=device,
            ))
            device.assert_not_called()

    def test_multiple_pending_journals_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            users = Path(directory)
            for name in ('one', 'two'):
                journal = (
                    users / name / 'AppData' / 'Local'
                    / 'PanasonicPostureManager' / 'native-display-slate.json'
                )
                journal.parent.mkdir(parents=True)
                journal.write_text(
                    '{"version":1,"original":false,"desired":true}',
                    encoding='utf-8',
                )
            with self.assertRaisesRegex(SlateError, 'multiple slate recovery journals'):
                read_bootstrap_slate_state(users_root=users, device_factory=Mock())

    def test_stale_slate_value_is_overridden_by_stable_laptop_geometry(self):
        now = [0.]

        class Sensor:
            def reading(self):
                return SensorReading(
                    (0., 0., -9.81), now[0], "iio"
                )

        baseline = StartupPhysicalBaseline(
            slate_reader=lambda: True,
            sensor=Sensor(),
            clock=lambda: now[0],
        )
        actual = None
        for _ in range(8):
            actual = baseline()
            now[0] += .1
        self.assertIs(actual, False)

    def test_tablet_like_geometry_does_not_override_an_ambiguous_slate_value(self):
        now = [0.]

        class Sensor:
            def reading(self):
                return SensorReading(
                    (0., 0., 9.81), now[0], "iio"
                )

        baseline = StartupPhysicalBaseline(
            slate_reader=lambda: True,
            sensor=Sensor(),
            clock=lambda: now[0],
        )
        for _ in range(8):
            self.assertIsNone(baseline())
            now[0] += .1

    def test_stable_tent_geometry_establishes_a_non_tablet_baseline(self):
        now = [0.]

        class Sensor:
            def reading(self):
                return SensorReading(
                    (0., -9.5, -2.), now[0], "iio"
                )

        baseline = StartupPhysicalBaseline(
            slate_reader=lambda: None,
            sensor=Sensor(),
            clock=lambda: now[0],
        )
        actual = None
        for _ in range(8):
            actual = baseline()
            now[0] += .1
        self.assertIs(actual, False)


if __name__ == '__main__':
    unittest.main()
