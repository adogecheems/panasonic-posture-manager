"""Coarse Windows direction acquisition and dwell without hardware or display writes."""
import unittest
from datetime import datetime, timezone

from panasonic_posture.windows.sensor import WindowsSensor, IDENTITY

from panasonic_posture.windows.orientation import (
    OrientationTracker, OrientationReading, OrientationError, QuadrantPolicy,
    SIMPLE_ORIENTATION_TRANSFORM,
)


def direction(value=0, **changes):
    result = dict(device='orientation-1', candidates=['orientation-1'], value=value, reading_transform=0)
    result.update(changes)
    return result


class OrientationTests(unittest.TestCase):
    def test_simple_orientation_uses_one_explicit_clockwise_transform_table(self):
        self.assertEqual(SIMPLE_ORIENTATION_TRANSFORM, (0, 3, 2, 1))

    def test_policy_requires_an_explicit_permutation_and_valid_timing(self):
        for mapping in (None, (0, 1, 2), (0, 0, 2, 3), (False, 1, 2, 3), (0, 1, 2, 4)):
            with self.assertRaises(ValueError): QuadrantPolicy(mapping=mapping)
        for value in (0, -1, True, float('inf'), 10**400):
            with self.assertRaises(ValueError): QuadrantPolicy(mapping=(0, 3, 2, 1), settle_seconds=value)

    def test_quadrants_and_face_states_keep_their_source_identity_and_sample_clock(self):
        tracker = OrientationTracker()
        for value in range(6):
            reading = tracker.accept(direction(value), stamp=50. + value)
            self.assertEqual((reading.value, reading.device, reading.stamp), (value, 'orientation-1', 50. + value))
        self.assertTrue(tracker.info.ready)

    def test_missing_ambiguous_changed_or_transformed_sources_are_rejected(self):
        for value in (None, direction(True), direction(6), direction(-1), direction(1.),
                      direction(candidates=[]), direction(candidates=['orientation-1', 'other']),
                      direction(device='other', candidates=['other']), direction(reading_transform=1),
                      direction(reading_transform=False)):
            tracker = OrientationTracker()
            tracker.accept(direction(), stamp=50.)
            with self.subTest(value=value), self.assertRaises(OrientationError): tracker.accept(value, stamp=51.)
            self.assertFalse(tracker.info.ready)
            self.assertIsNone(tracker.latest)

    def test_retry_preserves_identity_and_repeated_samples_cannot_change_direction(self):
        tracker = OrientationTracker()
        first = tracker.accept(direction(1), stamp=50.)
        self.assertEqual(tracker.accept(direction(3), stamp=50.), first)
        tracker.reset()
        self.assertIsNone(tracker.latest)
        with self.assertRaises(OrientationError): tracker.accept(direction(device='other', candidates=['other']), stamp=51.)
        tracker.accept(direction(), stamp=51.)
        for stamp in (50., float('nan'), True):
            with self.assertRaises(OrientationError): tracker.accept(direction(), stamp=stamp)

    def test_direction_requires_fresh_distinct_samples_for_the_full_dwell(self):
        policy = QuadrantPolicy(mapping=(0, 3, 2, 1), settle_seconds=.4, max_age=2.)
        sample = lambda value, stamp: OrientationReading(value, stamp, 'orientation-1')
        self.assertIsNone(policy.choose(sample(1, 10.), now=10.))
        self.assertIsNone(policy.choose(sample(1, 10.), now=10.8))
        self.assertEqual(policy.choose(sample(1, 10.5), now=10.8), 3)
        self.assertIsNone(policy.choose(sample(3, 11.), now=11.))
        self.assertEqual(policy.choose(sample(3, 11.5), now=11.5), 1)

    def test_faces_hold_current_direction_and_interrupt_a_pending_quadrant(self):
        policy = QuadrantPolicy(mapping=(0, 3, 2, 1), settle_seconds=.4, max_age=2.)
        sample = lambda value, stamp: OrientationReading(value, stamp, 'orientation-1')
        policy.choose(sample(0, 10.), now=10.)
        self.assertIsNone(policy.choose(sample(4, 10.5), now=10.5))
        self.assertIsNone(policy.choose(sample(5, 11.), now=11.))
        self.assertIsNone(policy.choose(sample(0, 11.5), now=11.5))
        self.assertEqual(policy.choose(sample(0, 12.), now=12.), 0)

    def test_loss_clock_reversal_and_large_gap_reset_pending_rotation(self):
        for bad, now in ((None, 11.), (OrientationReading(0, 8., 'orientation-1'), 11.),
                         (OrientationReading(0, 12., 'orientation-1'), 11.)):
            policy = QuadrantPolicy(mapping=(0, 3, 2, 1), settle_seconds=.4, max_age=2.)
            policy.choose(OrientationReading(0, 10., 'orientation-1'), now=10.)
            with self.assertRaises(OrientationError): policy.choose(bad, now=now)
            self.assertIsNone(policy.choose(OrientationReading(0, 11.5, 'orientation-1'), now=11.5))
        policy = QuadrantPolicy(mapping=(0, 3, 2, 1))
        policy.choose(OrientationReading(0, 10., 'orientation-1'), now=10.)
        self.assertIsNone(policy.choose(OrientationReading(0, 14., 'orientation-1'), now=14.))


class CompanionAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.now = 50.
        self.sensor = WindowsSensor(mount_matrix=IDENTITY, include_orientation=True, clock=lambda: self.now)
        self.addCleanup(self.sensor.close)

    def publish(self, stamp=1000., orientation=None, *, received=50., wall=1000.1):
        if isinstance(orientation, dict):
            orientation = dict(orientation)
            orientation.setdefault('observed_at', datetime.fromtimestamp(wall, timezone.utc).isoformat())
        frame = dict(version=1, device='accelerometer-1', candidates=['accelerometer-1'],
            minimum_report_interval_ms=10, x=0., y=0., z=-1.,
            timestamp=datetime.fromtimestamp(stamp, timezone.utc).isoformat(),
            observed_at=datetime.fromtimestamp(wall, timezone.utc).isoformat(), orientation=orientation)
        self.sensor._publish(frame, received, wall)
        return frame

    def test_source_clock_freshness_and_default_opt_in_are_independent_of_display(self):
        frame = self.publish(orientation=direction(4))
        self.assertTrue(self.sensor.info.ready)
        self.assertEqual(self.sensor.orientation().value, 4)
        self.assertAlmostEqual(self.sensor.orientation().stamp, self.now)
        self.assertLess(self.sensor.reading().stamp, self.sensor.orientation().stamp)
        self.assertTrue(self.sensor.orientation_info.ready)
        other = WindowsSensor(mount_matrix=IDENTITY, clock=lambda: self.now)
        self.addCleanup(other.close)
        other._publish(frame, 50., 1000.1)
        self.assertIsNone(other.orientation())
        self.now = 53.
        self.assertIsNone(self.sensor.orientation())
        self.assertFalse(self.sensor.orientation_info.ready)

    def test_optional_failure_does_not_break_posture_and_cached_acceleration_cannot_renew_direction(self):
        self.publish(orientation=direction(1))
        initial = self.sensor.orientation()
        self.now = 50.5
        self.publish(orientation=direction(3), received=50.5, wall=1000.6)
        self.assertEqual(self.sensor.orientation(), initial)
        self.publish(1000.5, orientation=direction(2, reading_transform=1), received=50.5, wall=1000.6)
        self.assertTrue(self.sensor.info.ready)
        self.assertIsNone(self.sensor.orientation())
        self.assertFalse(self.sensor.orientation_info.ready)

    def test_closed_manager_never_exposes_a_fresh_direction(self):
        self.publish(orientation=direction(0))
        self.sensor.close()
        self.assertIsNone(self.sensor.orientation())
        self.assertFalse(self.sensor.orientation_info.ready)

    def test_delayed_snapshot_keeps_its_observation_time_and_bad_clock_is_only_optional_failure(self):
        self.publish(1000., orientation=direction(1), received=50.8, wall=1000.8)
        self.now = 50.8
        self.assertAlmostEqual(self.sensor.orientation().stamp, 50.8)
        self.assertAlmostEqual(self.sensor.reading().stamp, 50.)
        self.publish(1000.9, orientation=direction(1, observed_at='broken'), received=51., wall=1001.)
        self.now = 51.
        self.assertTrue(self.sensor.info.ready)
        self.assertIsNone(self.sensor.orientation())


if __name__ == '__main__': unittest.main()
