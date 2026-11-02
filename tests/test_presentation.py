"""Tests for the presentation debouncer (sticky cap and settle behaviour)."""

import unittest

from panasonic_posture.model import Pose
from panasonic_posture.policy import PolicyConfig
from panasonic_posture.presentation import PosePresentation


class PresentationTestCase(unittest.TestCase):
    def setUp(self):
        self.presentation = PosePresentation()


class BasicBehaviourTests(PresentationTestCase):
    def test_initial_value_is_unknown(self):
        self.assertEqual(self.presentation.value, Pose.UNKNOWN)
        self.assertEqual(self.presentation.stale_seconds, PolicyConfig().stale_seconds)
        self.assertEqual(self.presentation.settle_seconds, 0.3)
        self.assertEqual(self.presentation.unknown_sticky_seconds, 1.0)

    def test_moving_does_not_insert_unknown_drawing(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        self.assertEqual(p.update(0.7, Pose.LAPTOP, False, 0.7), Pose.LAPTOP)
        self.assertEqual(p.update(0.8, Pose.UNKNOWN, False, 0.8), Pose.LAPTOP)
        self.assertEqual(p.update(1.0, Pose.TENT, False, 1.0), Pose.LAPTOP)
        self.assertEqual(p.update(1.7, Pose.TENT, False, 1.7), Pose.TENT)

    def test_short_candidate_is_never_shown(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.7, Pose.LAPTOP, False, 0.7)
        p.update(0.8, Pose.TENT, False, 0.8)
        p.update(1.0, Pose.LAPTOP, False, 1.0)
        self.assertEqual(p.value, Pose.LAPTOP)

    def test_intermediate_missing_sample_does_not_restart_confirmation(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.4, Pose.UNKNOWN, False, 0.4)
        self.assertEqual(p.update(0.7, Pose.LAPTOP, False, 0.7), Pose.LAPTOP)

    def test_candidate_change_restarts_settle_timer(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.7, Pose.LAPTOP, False, 0.7)
        p.update(0.8, Pose.TENT, False, 0.8)
        p.update(1.0, Pose.STAND, False, 1.0)
        self.assertEqual(p.update(1.2, Pose.STAND, False, 1.2), Pose.LAPTOP)
        self.assertEqual(p.update(1.4, Pose.STAND, False, 1.4), Pose.STAND)

    def test_accepts_string_candidates(self):
        p = self.presentation
        p.update(0.0, "laptop", False, 0.0)
        self.assertEqual(p.update(0.5, "laptop", False, 0.5), Pose.LAPTOP)
        self.assertIsInstance(p.update(0.6, Pose.LAPTOP, False, 0.6), Pose)

    def test_unknown_string_rejected(self):
        with self.assertRaises(ValueError):
            self.presentation.update(0.0, "sideways", False, 0.0)


class HardwareOverrideTests(PresentationTestCase):
    def test_hardware_confirmation_has_no_presentation_delay(self):
        p = self.presentation
        self.assertEqual(p.update(0.0, Pose.UNKNOWN, True, None), Pose.TABLET)
        self.assertEqual(p.update(0.1, Pose.UNKNOWN, False, 0.1), Pose.TABLET)
        p.update(0.2, Pose.STAND, False, 0.2)
        self.assertEqual(p.update(0.9, Pose.STAND, False, 0.9), Pose.STAND)

    def test_hardware_tablet_ignores_stale_stamp(self):
        p = self.presentation
        self.assertEqual(p.update(10.0, Pose.UNKNOWN, True, None), Pose.TABLET)
        self.assertEqual(p.update(10.1, Pose.LAPTOP, True, 0.0), Pose.TABLET)

    def test_hardware_release_starts_unknown_sticky_window(self):
        p = self.presentation
        p.update(0.0, Pose.UNKNOWN, True, None)
        self.assertEqual(p.update(0.1, Pose.UNKNOWN, False, 0.1), Pose.TABLET)
        self.assertEqual(p.update(0.9, Pose.UNKNOWN, False, 0.9), Pose.TABLET)
        self.assertEqual(p.update(1.2, Pose.UNKNOWN, False, 1.2), Pose.UNKNOWN)


class StalenessTests(PresentationTestCase):
    def test_true_staleness_is_not_hidden(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.7, Pose.LAPTOP, False, 0.7)
        self.assertEqual(p.update(3.0, Pose.LAPTOP, False, 0.7), Pose.UNKNOWN)

    def test_missing_stamp_means_unknown(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.5, Pose.LAPTOP, False, 0.5)
        self.assertEqual(p.update(0.6, Pose.LAPTOP, False, None), Pose.UNKNOWN)
        self.assertEqual(p.value, Pose.UNKNOWN)

    def test_stale_window_is_configurable(self):
        p = PosePresentation(config=PolicyConfig(stale_seconds=0.5))
        self.assertEqual(p.stale_seconds, 0.5)
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.5, Pose.LAPTOP, False, 0.5)
        # now - stamp == 0.6 > 0.5
        self.assertEqual(p.update(1.1, Pose.LAPTOP, False, 0.5), Pose.UNKNOWN)

    def test_constructor_overrides(self):
        p = PosePresentation(settle_seconds=0.5, stale_seconds=3.0)
        self.assertEqual(p.settle_seconds, 0.5)
        self.assertEqual(p.stale_seconds, 3.0)
        self.assertEqual(p.config, PolicyConfig())


class UnknownStickyCapTests(PresentationTestCase):
    def test_unknown_is_sticky_for_at_most_one_second(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.3, Pose.LAPTOP, False, 0.3)
        self.assertEqual(p.value, Pose.LAPTOP)
        self.assertEqual(p.update(0.4, Pose.UNKNOWN, False, 0.4), Pose.LAPTOP)
        # 0.9 s after the first unknown report the drawing is still kept.
        self.assertEqual(p.update(1.3, Pose.UNKNOWN, False, 1.3), Pose.LAPTOP)
        # The sticky limit is one second; slightly beyond it the drawing resets.
        self.assertEqual(p.update(1.5, Pose.UNKNOWN, False, 1.5), Pose.UNKNOWN)

    def test_sticky_counter_restarts_after_a_confirmed_pose(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.3, Pose.LAPTOP, False, 0.3)
        p.update(0.4, Pose.UNKNOWN, False, 0.4)
        p.update(1.0, Pose.LAPTOP, False, 1.0)  # confirmed again, resets sticky
        self.assertEqual(p.update(1.1, Pose.UNKNOWN, False, 1.1), Pose.LAPTOP)
        self.assertEqual(p.update(1.9, Pose.UNKNOWN, False, 1.9), Pose.LAPTOP)
        self.assertEqual(p.update(2.2, Pose.UNKNOWN, False, 2.2), Pose.UNKNOWN)

    def test_new_pose_after_sticky_expiry_is_shown(self):
        p = self.presentation
        p.update(0.0, Pose.LAPTOP, False, 0.0)
        p.update(0.3, Pose.LAPTOP, False, 0.3)
        self.assertEqual(p.update(0.4, Pose.UNKNOWN, False, 0.4), Pose.LAPTOP)
        self.assertEqual(p.update(1.5, Pose.UNKNOWN, False, 1.5), Pose.UNKNOWN)
        p.update(2.0, Pose.TENT, False, 2.0)
        self.assertEqual(p.update(2.4, Pose.TENT, False, 2.4), Pose.TENT)


if __name__ == "__main__":
    unittest.main()
