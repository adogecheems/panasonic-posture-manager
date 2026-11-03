"""Tests for the pure posture policy state machine.

Covers the review fixes owned by the domain layer:
P1-3 (bounded history, including hardware tablet mode), P2-5 (invalid samples
must not flash the mode or cancel the tent prompt), P2-6 (confirm only inside
the documented tent zone) and P2-7 (suspend reset / epoch invalidation).
"""

import math
import unittest

from panasonic_posture.model import SensorReading, SensorSource
from panasonic_posture.policy import (
    MODE_DISABLED,
    MODE_HARDWARE,
    MODE_LAPTOP,
    MODE_STAND,
    MODE_STAND_PROMPT,
    MODE_TENT,
    MODE_TENT_PROMPT,
    Policy,
    PolicyConfig,
    geometry,
    in_tent_zone,
)
from panasonic_posture.protocol import IIO_NO_SAMPLE_SECONDS

GRAVITY = 9.80665


def accel_vector(pitch=0.0, magnitude=GRAVITY, roll=0.0):
    """Gravity vector in the device frame for a pitch/roll pose."""

    roll_radians = math.radians(roll)
    pitch_radians = math.radians(pitch)
    return (
        magnitude * math.sin(roll_radians),
        -magnitude * math.cos(roll_radians) * math.sin(pitch_radians),
        -magnitude * math.cos(roll_radians) * math.cos(pitch_radians),
    )


def sample(pitch=0.0, magnitude=GRAVITY, roll=0.0, stamp=0.0):
    return SensorReading(
        vector=accel_vector(pitch, magnitude, roll),
        stamp=stamp,
        source=SensorSource.IIO,
    )


def invalid_sample(kind):
    if kind == "missing":
        return SensorReading(vector=None, stamp=None, source=SensorSource.IIO)
    if kind == "zero":
        return SensorReading(vector=(0.0, 0.0, 0.0), stamp=0.0, source=SensorSource.IIO)
    if kind == "nan":
        return SensorReading(
            vector=(float("nan"), 0.0, -GRAVITY), stamp=0.0, source=SensorSource.IIO
        )
    if kind == "overrange":
        return SensorReading(
            vector=accel_vector(0.0, 20.0), stamp=0.0, source=SensorSource.IIO
        )
    if kind == "underrange":
        return SensorReading(
            vector=accel_vector(0.0, 2.0), stamp=0.0, source=SensorSource.IIO
        )
    raise AssertionError(f"unknown invalid kind {kind!r}")


INVALID_KINDS = ("missing", "zero", "nan", "overrange", "underrange")


class GeometryTests(unittest.TestCase):
    def test_flat_pose(self):
        unit, magnitude, pitch, roll, flat = geometry((0.0, 0.0, -GRAVITY))
        self.assertEqual(unit, (0.0, 0.0, -1.0))
        self.assertAlmostEqual(magnitude, GRAVITY)
        self.assertEqual(pitch, 0.0)
        self.assertEqual(roll, 0.0)
        self.assertEqual(flat, 0.0)

    def test_tent_pose(self):
        _, magnitude, pitch, roll, flat = geometry(accel_vector(80.0))
        self.assertAlmostEqual(magnitude, GRAVITY)
        self.assertAlmostEqual(pitch, 80.0)
        self.assertAlmostEqual(roll, 0.0)
        self.assertAlmostEqual(flat, 80.0)

    def test_sideways_has_no_pitch(self):
        _, _, pitch, roll, flat = geometry((GRAVITY, 0.0, 0.0))
        self.assertIsNone(pitch)
        self.assertAlmostEqual(roll, 90.0)
        self.assertAlmostEqual(flat, 90.0)

    def test_invalid_input_raises_value_error(self):
        for bad in (
            None,
            (),
            (1.0, 2.0),
            (float("nan"), 0.0, 0.0),
            (float("inf"), 0.0, 0.0),
            (0.0, 0.0, 0.0),
            "not-a-vector",
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    geometry(bad)

    def test_tent_zone_boundaries(self):
        self.assertFalse(in_tent_zone(None, 0.0))
        self.assertTrue(in_tent_zone(65.0, 0.0))
        self.assertTrue(in_tent_zone(115.0, 0.0))
        self.assertTrue(in_tent_zone(80.0, 25.0))
        self.assertTrue(in_tent_zone(80.0, -25.0))
        self.assertFalse(in_tent_zone(80.0, 25.5))
        self.assertFalse(in_tent_zone(64.9, 0.0))
        self.assertFalse(in_tent_zone(115.1, 0.0))
        self.assertFalse(in_tent_zone(45.0, 0.0))
        self.assertFalse(in_tent_zone(150.0, 0.0))


class PolicyTestCase(unittest.TestCase):
    def setUp(self):
        self.config = PolicyConfig()
        self.policy = Policy(self.config)
        self.now = 0.0

    # -- helpers ---------------------------------------------------------

    def step(
        self,
        pitch=0.0,
        hardware=False,
        magnitude=GRAVITY,
        roll=0.0,
        dt=0.1,
        tent_prompt=True,
        stand_prompt=False,
    ):
        self.now += dt
        self.policy.update(
            self.now,
            sample(pitch, magnitude, roll, stamp=self.now),
            hardware,
            tent_prompt=tent_prompt,
            stand_prompt=stand_prompt,
        )
        return self.policy.mode

    def hold(
        self,
        pitch=0.0,
        hardware=False,
        seconds=2.5,
        magnitude=GRAVITY,
        roll=0.0,
        dt=0.1,
        tent_prompt=True,
        stand_prompt=False,
    ):
        for _ in range(max(1, round(seconds / dt))):
            self.step(
                pitch,
                hardware,
                magnitude,
                roll,
                dt,
                tent_prompt,
                stand_prompt,
            )
        return self.policy.mode

    def feed_invalid(self, kind, hardware=False, dt=0.1):
        self.now += dt
        self.policy.update(self.now, invalid_sample(kind), hardware)
        return self.policy.mode


class InitialStateTests(PolicyTestCase):
    def test_initial_state_is_disabled(self):
        self.assertEqual(self.policy.mode, MODE_DISABLED)
        self.assertEqual(self.policy.epoch, 0)
        self.assertFalse(self.policy.stable)
        self.assertFalse(self.policy.fresh)
        self.assertEqual(len(self.policy.history), 0)
        self.assertIsNone(self.policy.pending_notification)
        self.assertIsNone(self.policy.last_stamp)
        self.assertIsNone(self.policy.last_geometry)

    def test_config_defaults_are_used(self):
        self.assertEqual(self.policy.config, PolicyConfig())
        self.assertGreater(self.policy.config.stale_seconds, 0)

    def test_iio_failover_starts_before_the_policy_stale_window(self):
        # Regression: both deadlines used to be 2.0 seconds.  On a stalled IIO
        # stream the policy therefore marked the target unavailable before the
        # sysfs fallback could provide a replacement sample.
        config = PolicyConfig()
        self.assertLess(IIO_NO_SAMPLE_SECONDS, config.stale_seconds)
        self.assertGreaterEqual(
            config.stale_seconds - IIO_NO_SAMPLE_SECONDS, 2.0
        )

    def test_controller_style_injection(self):
        config = PolicyConfig(stale_seconds=1.0)
        policy = Policy(config)
        self.assertIs(policy.config, config)


class PostureClassificationTests(PolicyTestCase):
    def test_laptop_posture(self):
        self.hold(0.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.assertTrue(self.policy.stable)

    def test_tent_requires_confirmation_and_latches(self):
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        epoch = self.policy.epoch
        self.assertEqual(self.policy.pending_notification, epoch)
        self.assertTrue(self.policy.confirm(epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.assertIsNone(self.policy.pending_notification)
        self.hold(55.0)
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.hold(0.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_opposite_vertical_is_not_tent(self):
        self.hold(-90.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_unknown_hardware_blocks(self):
        self.hold(180.0, hardware=None)
        self.assertEqual(self.policy.mode, MODE_DISABLED)

    def test_hardware_wins_without_sensor(self):
        self.policy.update(4.0, SensorReading(), True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        self.policy.update(40.0, SensorReading(), True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)

    def test_full_fold_and_stand_handoff(self):
        self.hold(180.0, hardware=True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        self.hold(175.0, hardware=False)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.hold(0.0, hardware=False)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_old_notification_cannot_confirm(self):
        self.hold(80.0)
        stale_epoch = self.policy.epoch
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.assertFalse(self.policy.confirm(stale_epoch))
        self.assertEqual(self.policy.mode, MODE_STAND)

    def test_sensor_timeout_releases_software_only(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.now += self.config.stale_seconds + 0.1
        self.policy.update(self.now, SensorReading(), False)
        self.assertEqual(self.policy.mode, MODE_DISABLED)

    def test_stand_survives_an_iio_to_sysfs_handoff(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)

        # Live traces showed a 2.3 second IIO silence before sysfs supplied
        # the next sample.  That must not release the stand takeover, even
        # though it exceeds the old two-second stale window.
        for _ in range(23):
            self.feed_invalid("missing")
        self.assertEqual(self.policy.mode, MODE_STAND)

        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)

    def test_wrap_does_not_exit_stand(self):
        self.hold(179.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        for _ in range(30):
            self.step(-179.0)
            self.step(179.0)
        self.assertEqual(self.policy.mode, MODE_STAND)

    def test_sideways_and_invalid_force_do_not_trigger(self):
        for _ in range(30):
            self.now += 0.1
            self.policy.update(
                self.now,
                SensorReading(
                    vector=(GRAVITY, 0.0, 0.0),
                    stamp=self.now,
                    source=SensorSource.IIO,
                ),
                False,
            )
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.hold(80.0, magnitude=20.0, seconds=1.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_motion_and_short_visit_do_not_prompt(self):
        for index in range(60):
            self.step(68.0 if index % 2 else 110.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.hold(80.0, seconds=0.5)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_hold_hysteresis(self):
        self.hold(160.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.hold(145.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.hold(120.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_stand_noise_does_not_restart_the_takeover(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)

        # The shared sensor can return a short run below the old 140 degree
        # hold edge.  It must not release S and immediately rebuild the relay.
        for _ in range(12):
            self.step(120.0)
            self.assertEqual(self.policy.mode, MODE_STAND)

        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        # A real departure is still detected after the stand-specific debounce.
        self.hold(120.0, seconds=2.2)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_stand_holds_at_the_wider_hold_edge(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.hold(135.0)
        self.assertEqual(self.policy.mode, MODE_STAND)

    def test_stand_does_not_leave_on_an_unstable_departure(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)

        # Rapid, alternating samples below the hold edge look like motion or
        # sensor noise, not a settled laptop posture.  They must not release
        # the takeover even when the excursion lasts longer than leave_seconds.
        for _ in range(30):
            self.step(100.0)
            self.step(125.0)
            self.assertEqual(self.policy.mode, MODE_STAND)

        # A settled departure is still accepted.
        self.hold(120.0, seconds=self.config.stand_leave_seconds + 0.2)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_stand_to_tent_enters_without_prompt(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        modes = [self.step(80.0) for _ in range(30)]
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.assertNotIn(MODE_TENT_PROMPT, modes)
        self.assertIsNone(self.policy.pending_notification)

    def test_direct_tent_does_not_disable_later_laptop_prompt(self):
        self.hold(175.0)
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.hold(0.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.hold(0.0, seconds=5.2)
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)

    def test_hardware_release_to_tent_enters_without_prompt(self):
        self.hold(80.0, hardware=True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        modes = [self.step(80.0, hardware=False) for _ in range(30)]
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.assertNotIn(MODE_TENT_PROMPT, modes)

    def test_hardware_release_through_intermediate_pose_keeps_direct_tent(self):
        self.hold(180.0, hardware=True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)

        # The switch can turn off before the hinge reaches the tent zone.  A
        # transient L must not turn the rest of the fold into a laptop-origin
        # tent transition that prompts again.
        self.step(30.0, hardware=False)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        modes = [self.step(80.0) for _ in range(30)]

        self.assertEqual(self.policy.mode, MODE_TENT)
        self.assertNotIn(MODE_TENT_PROMPT, modes)

    def test_hardware_release_after_settled_laptop_still_prompts(self):
        self.hold(180.0, hardware=True)
        self.hold(0.0, hardware=False)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)

    def test_epoch_changes_only_on_mode_change(self):
        self.hold(0.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        epoch = self.policy.epoch
        self.hold(0.0, seconds=2.0)
        self.assertEqual(self.policy.epoch, epoch)
        self.hold(0.0, hardware=True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        self.assertGreater(self.policy.epoch, epoch)

    def test_entering_hardware_cancels_prompt(self):
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        epoch = self.policy.epoch
        self.assertIsNotNone(self.policy.pending_notification)
        self.step(80.0, hardware=True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        self.assertGreater(self.policy.epoch, epoch)
        self.assertIsNone(self.policy.pending_notification)
        self.assertEqual(len(self.policy.history), 0)

    def test_leaving_hardware_restarts_stable_run(self):
        self.hold(180.0, hardware=True)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        self.now += 0.1
        self.policy.update(self.now, sample(0.0, stamp=self.now), False)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.assertFalse(self.policy.stable)
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)

    def test_slow_sensor_cadence_can_enter_stand(self):
        policy = Policy()
        stamp = 0.0
        for tick in range(60):
            now = tick * 0.1
            if tick % 7 == 0:
                stamp = now
            policy.update(now, sample(180.0, stamp=stamp), False)
        self.assertEqual(policy.mode, MODE_STAND)

    def test_slow_sensor_cadence_can_offer_tent(self):
        policy = Policy()
        stamp = 0.0
        for tick in range(60):
            now = tick * 0.1
            if tick % 7 == 0:
                stamp = now
            policy.update(now, sample(80.0, stamp=stamp), False)
        self.assertEqual(policy.mode, MODE_TENT_PROMPT)


class StandPromptTests(PolicyTestCase):
    def test_prompt_is_opt_in_and_keeps_previous_behavior_by_default(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.assertIsNone(self.policy.pending_prompt)
    def test_stand_prompt_waits_for_confirmation(self):
        self.hold(175.0, stand_prompt=True)
        self.assertEqual(self.policy.mode, MODE_STAND_PROMPT)
        self.assertEqual(self.policy.pending_prompt, "stand")
        self.assertEqual(self.policy.pending_notification, self.policy.epoch)
        self.assertFalse(self.policy.confirm_stand(self.policy.epoch + 1))
        self.assertTrue(self.policy.confirm_stand(self.policy.epoch))
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.assertIsNone(self.policy.pending_prompt)

    def test_confirm_requires_current_stand_zone(self):
        self.hold(175.0, stand_prompt=True)
        self.assertEqual(self.policy.mode, MODE_STAND_PROMPT)
        epoch = self.policy.epoch
        self.hold(150.0, stand_prompt=True)
        self.assertEqual(self.policy.mode, MODE_STAND_PROMPT)
        self.assertFalse(self.policy.confirm_stand(epoch))
        self.hold(175.0, stand_prompt=True)
        self.assertTrue(self.policy.confirm_stand(self.policy.epoch))
        self.assertEqual(self.policy.mode, MODE_STAND)

    def test_stand_prompt_is_invalidated_outside_hold_band(self):
        self.hold(175.0, stand_prompt=True)
        self.assertEqual(self.policy.mode, MODE_STAND_PROMPT)
        epoch = self.policy.epoch
        self.hold(120.0, stand_prompt=True)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.assertFalse(self.policy.confirm_stand(epoch))

    def test_disabling_prompt_applies_stand_without_another_confirmation(self):
        self.hold(175.0, stand_prompt=True)
        self.assertEqual(self.policy.mode, MODE_STAND_PROMPT)
        self.hold(175.0, stand_prompt=False)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.assertIsNone(self.policy.pending_prompt)

    def test_stand_prompt_only_applies_to_laptop_to_stand_transition(self):
        self.hold(80.0)
        self.assertTrue(self.policy.confirm(self.policy.epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.hold(175.0, stand_prompt=True)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.assertIsNone(self.policy.pending_prompt)


class TentPromptSettingTests(PolicyTestCase):
    def test_disabled_prompt_enters_tent_from_laptop(self):
        self.assertEqual(self.hold(80.0, tent_prompt=False), MODE_TENT)
        self.assertIsNone(self.policy.pending_prompt)

    def test_disabling_prompt_resolves_an_existing_tent_prompt(self):
        self.assertEqual(self.hold(80.0), MODE_TENT_PROMPT)
        self.assertEqual(self.hold(80.0, tent_prompt=False), MODE_TENT)
        self.assertIsNone(self.policy.pending_prompt)

    def test_disabled_prompt_does_not_skip_non_laptop_direct_entry_rules(self):
        self.assertEqual(self.hold(80.0, tent_prompt=False), MODE_TENT)
        self.assertEqual(self.hold(175.0, tent_prompt=False), MODE_STAND)
        self.assertEqual(self.hold(80.0, tent_prompt=False), MODE_TENT)
        self.assertIsNone(self.policy.pending_notification)


class TentReentryTests(PolicyTestCase):
    def confirm_tent(self):
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        self.assertTrue(self.policy.confirm(self.policy.epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)

    def leave_tent(self):
        self.hold(0.0, seconds=1.2)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)

    def test_recent_reentry_restores_tent_without_prompt(self):
        self.confirm_tent()
        self.leave_tent()
        modes = [self.step(80.0) for _ in range(30)]
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.assertNotIn(MODE_TENT_PROMPT, modes)
        self.assertIsNone(self.policy.pending_notification)

    def test_reentry_after_grace_requires_confirmation(self):
        self.confirm_tent()
        self.leave_tent()
        self.hold(0.0, seconds=5.2)
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        self.assertIsNotNone(self.policy.pending_notification)

    def test_unconfirmed_tent_does_not_open_reentry_window(self):
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        self.hold(0.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)

    def test_reset_closes_reentry_window(self):
        self.confirm_tent()
        self.leave_tent()
        self.policy.reset_after_suspend()
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)

    def test_hardware_takeover_closes_reentry_window(self):
        self.confirm_tent()
        self.hold(80.0, hardware=True, seconds=0.2)
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        self.hold(0.0, hardware=False)
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)


class TentConfirmationZoneTests(PolicyTestCase):
    def test_confirm_requires_current_tent_geometry(self):
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        # Inside the wide hold band but outside the documented tent zone.
        for angle in (45.0, 60.0, 120.0, 150.0):
            with self.subTest(angle=angle):
                if self.policy.mode != MODE_TENT_PROMPT:
                    # The 45 deg hold-band boundary may legitimately drop back
                    # to laptop after the smoothing filter; rebuild the prompt.
                    self.hold(80.0)
                    self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
                epoch = self.policy.epoch
                self.hold(angle, seconds=1.5)
                self.assertFalse(self.policy.confirm(epoch))
        if self.policy.mode != MODE_TENT_PROMPT:
            self.hold(80.0)
        self.hold(80.0, seconds=2.5)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        self.assertTrue(self.policy.confirm(self.policy.epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)

    def test_confirm_rejects_wrong_epoch(self):
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        epoch = self.policy.epoch
        self.assertFalse(self.policy.confirm(epoch + 1))
        self.assertFalse(self.policy.confirm(epoch - 1))
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)

    def test_confirm_rejects_hardware_and_wrong_mode(self):
        self.hold(80.0)
        epoch = self.policy.epoch
        self.policy.update(self.now + 0.1, sample(80.0, stamp=self.now + 0.1), True)
        self.now += 0.1
        self.assertEqual(self.policy.mode, MODE_HARDWARE)
        self.assertFalse(self.policy.confirm(epoch))
        self.assertFalse(self.policy.confirm(self.policy.epoch))

    def test_confirm_after_leaving_hold_band(self):
        self.hold(80.0)
        epoch = self.policy.epoch
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.assertFalse(self.policy.confirm(epoch))

    def test_second_confirm_is_rejected(self):
        self.hold(80.0)
        epoch = self.policy.epoch
        self.assertTrue(self.policy.confirm(epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)
        self.assertFalse(self.policy.confirm(epoch))


class InvalidSampleTests(PolicyTestCase):
    def test_single_invalid_sample_keeps_stand(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        epoch = self.policy.epoch
        anchor_stamp = self.policy.last_stamp
        anchor_geometry = self.policy.last_geometry
        for kind in INVALID_KINDS:
            with self.subTest(kind=kind):
                self.feed_invalid(kind)
                self.assertEqual(self.policy.mode, MODE_STAND)
                self.assertEqual(self.policy.epoch, epoch)
        self.assertEqual(self.policy.last_stamp, anchor_stamp)
        self.assertEqual(self.policy.last_geometry, anchor_geometry)

    def test_single_invalid_sample_keeps_tent_prompt(self):
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        epoch = self.policy.epoch
        anchor_geometry = self.policy.last_geometry
        for kind in INVALID_KINDS:
            with self.subTest(kind=kind):
                self.feed_invalid(kind)
                self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
                self.assertEqual(self.policy.epoch, epoch)
                self.assertEqual(self.policy.pending_notification, epoch)
                self.assertEqual(self.policy.last_geometry, anchor_geometry)
        # The prompt is still the current one, so it remains confirmable.
        self.assertTrue(self.policy.confirm(epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)

    def test_single_invalid_sample_keeps_confirmed_tent(self):
        self.hold(80.0)
        self.assertTrue(self.policy.confirm(self.policy.epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)
        epoch = self.policy.epoch
        for kind in INVALID_KINDS:
            with self.subTest(kind=kind):
                self.feed_invalid(kind)
                self.assertEqual(self.policy.mode, MODE_TENT)
                self.assertEqual(self.policy.epoch, epoch)

    def test_invalid_samples_do_not_move_stable_anchor(self):
        self.hold(80.0)
        self.assertTrue(self.policy.stable)
        stable_since = self.policy.stable_since
        last_sample = self.policy.last_sample
        for kind in INVALID_KINDS:
            self.feed_invalid(kind)
        self.assertEqual(self.policy.stable_since, stable_since)
        self.assertEqual(self.policy.last_sample, last_sample)

    def test_sustained_invalid_enters_disabled_after_stale_window(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        # Still inside the stale window: a single bad frame never releases.
        stale_steps = math.ceil(self.config.stale_seconds / 0.1)
        for _ in range(stale_steps):
            self.feed_invalid("missing")
        self.assertEqual(self.policy.mode, MODE_STAND)
        # More than stale_seconds without a valid sample: disabled.
        self.feed_invalid("missing")
        self.assertEqual(self.policy.mode, MODE_DISABLED)

    def test_sustained_invalid_clears_all_anchors(self):
        stale_steps = math.ceil(self.config.stale_seconds / 0.1)
        self.hold(175.0)
        for _ in range(stale_steps + 1):
            self.feed_invalid("zero")
        self.assertEqual(self.policy.mode, MODE_DISABLED)
        self.assertEqual(len(self.policy.history), 0)
        self.assertIsNone(self.policy.last_stamp)
        self.assertIsNone(self.policy.last_geometry)
        self.assertIsNone(self.policy.stable_since)
        self.assertFalse(self.policy.stable)
        self.assertFalse(self.policy.fresh)

    def test_recovery_after_stale_window_rebuilds_run(self):
        stale_steps = math.ceil(self.config.stale_seconds / 0.1)
        self.hold(175.0)
        for _ in range(stale_steps + 1):
            self.feed_invalid("nan")
        self.assertEqual(self.policy.mode, MODE_DISABLED)
        self.hold(0.0)
        self.assertEqual(self.policy.mode, MODE_LAPTOP)
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)


class BoundedHistoryTests(unittest.TestCase):
    """P1-3: the smoothing history must stay bounded on every return path."""

    def test_hardware_mode_history_is_bounded(self):
        policy = Policy()
        now = 0.0
        for _ in range(30000):
            now += 0.01
            policy.update(now, sample(0.0, stamp=now), True)
        self.assertEqual(policy.mode, MODE_HARDWARE)
        self.assertLessEqual(len(policy.history), policy.config.history_max_samples)
        self.assertEqual(len(policy.history), 0)

    def test_software_mode_history_is_bounded(self):
        policy = Policy()
        now = 0.0
        for _ in range(30000):
            now += 0.01
            policy.update(now, sample(0.0, stamp=now), False)
        self.assertEqual(policy.mode, MODE_LAPTOP)
        self.assertLessEqual(len(policy.history), policy.config.history_max_samples)

    def test_history_time_window_prunes_old_samples(self):
        policy = Policy()
        for index in range(30):
            policy.update(
                index * 0.1, sample(0.0, stamp=index * 0.1), False
            )
        # 0.75 s window at 10 Hz keeps at most 9 samples.
        self.assertLessEqual(len(policy.history), 9)

    def test_history_is_bounded_on_all_state_paths(self):
        policy = Policy()
        limit = policy.config.history_max_samples
        now = 0.0

        def feed(pitch, hardware, steps, dt=0.05):
            nonlocal now
            for _ in range(steps):
                now += dt
                policy.update(now, sample(pitch, stamp=now), hardware)
                self.assertLessEqual(len(policy.history), limit)

        feed(0.0, False, 200)   # laptop
        feed(80.0, False, 200)  # tent prompt
        feed(175.0, False, 200)  # stand
        feed(180.0, True, 200)  # hardware tablet
        feed(0.0, None, 100)  # hardware unknown -> disabled
        for _ in range(120):  # sustained invalid -> disabled
            now += 0.05
            policy.update(now, SensorReading(), False)
            self.assertLessEqual(len(policy.history), limit)
        self.assertEqual(policy.mode, MODE_DISABLED)


class SuspendResetTests(PolicyTestCase):
    def test_reset_clears_anchors_and_disables(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.policy.reset_after_suspend()
        self.assertEqual(self.policy.mode, MODE_DISABLED)
        self.assertEqual(len(self.policy.history), 0)
        self.assertIsNone(self.policy.last_stamp)
        self.assertIsNone(self.policy.last_geometry)
        self.assertIsNone(self.policy.stable_since)
        self.assertFalse(self.policy.stable)
        self.assertFalse(self.policy.fresh)
        self.assertIsNone(self.policy.hardware)

    def test_reset_invalidates_old_prompt_epoch(self):
        self.hold(80.0)
        epoch = self.policy.epoch
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        self.policy.reset_after_suspend()
        self.assertEqual(self.policy.epoch, epoch + 1)
        self.assertFalse(self.policy.confirm(epoch))
        # A fresh, fully rebuilt prompt is confirmable again.
        self.hold(80.0)
        self.assertEqual(self.policy.mode, MODE_TENT_PROMPT)
        self.assertGreater(self.policy.epoch, epoch)
        self.assertTrue(self.policy.confirm(self.policy.epoch))
        self.assertEqual(self.policy.mode, MODE_TENT)

    def test_reset_prevents_stale_mode_rebuild(self):
        self.hold(175.0)
        self.assertEqual(self.policy.mode, MODE_STAND)
        self.policy.reset_after_suspend()
        # Without new samples the old stand evidence must not come back.
        self.now += 0.1
        self.policy.update(self.now, SensorReading(), False)
        self.assertEqual(self.policy.mode, MODE_DISABLED)

    def test_reset_increments_epoch_every_call(self):
        epoch = self.policy.epoch
        self.policy.reset_after_suspend()
        self.assertEqual(self.policy.epoch, epoch + 1)
        self.policy.reset_after_suspend()
        self.assertEqual(self.policy.epoch, epoch + 2)


class ConfigTests(PolicyTestCase):
    def test_config_controls_enter_timer(self):
        config = PolicyConfig(enter_seconds=1.5)
        policy = Policy(config)
        now = 0.0
        for _ in range(10):
            now += 0.1
            policy.update(now, sample(175.0, stamp=now), False)
        self.assertEqual(policy.mode, MODE_LAPTOP)
        for _ in range(15):
            now += 0.1
            policy.update(now, sample(175.0, stamp=now), False)
        self.assertEqual(policy.mode, MODE_STAND)

    def test_config_controls_stale_window(self):
        config = PolicyConfig(stale_seconds=0.5)
        policy = Policy(config)
        now = 0.0
        for _ in range(30):
            now += 0.1
            policy.update(now, sample(175.0, stamp=now), False)
        self.assertEqual(policy.mode, MODE_STAND)
        for _ in range(4):
            now += 0.1
            policy.update(now, SensorReading(), False)
        self.assertEqual(policy.mode, MODE_STAND)
        for _ in range(2):
            now += 0.1
            policy.update(now, SensorReading(), False)
        self.assertEqual(policy.mode, MODE_DISABLED)

    def test_config_rejects_invalid_values(self):
        for kwargs in (
            {"enter_seconds": -1.0},
            {"tent_reentry_seconds": -1.0},
            {"stand_leave_seconds": -1.0},
            {"stale_seconds": float("nan")},
            {"history_seconds": -0.1},
            {"history_max_samples": 0},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    PolicyConfig(**kwargs)


if __name__ == "__main__":
    unittest.main()
