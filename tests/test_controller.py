"""Tests for the pure controller (target decisions + generation transaction)."""

import math
import unittest

from panasonic_posture.controller import Controller
from panasonic_posture.model import Mode, SensorReading, SensorSource, Target
from panasonic_posture.policy import (
    MODE_DISABLED,
    MODE_STAND,
    MODE_STAND_PROMPT,
    MODE_TENT,
    MODE_TENT_PROMPT,
    Policy,
    PolicyConfig,
)
from panasonic_posture.protocol import ErrorCode

GRAVITY = 9.80665


def accel_vector(pitch=0.0, magnitude=GRAVITY, roll=0.0):
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


class ControllerTestCase(unittest.TestCase):
    def setUp(self):
        self.controller = Controller()
        self.now = 0.0

    def step(self, pitch=0.0, hardware=False, dt=0.1):
        self.now += dt
        return self.controller.update(
            self.now, sample(pitch, stamp=self.now), hardware
        )

    def hold(self, pitch=0.0, hardware=False, seconds=2.5, dt=0.1):
        result = (self.controller.generation, self.controller.target)
        for _ in range(max(1, round(seconds / dt))):
            result = self.step(pitch, hardware, dt)
        return result


class InitialStateTests(ControllerTestCase):
    def test_initial_state(self):
        controller = self.controller
        self.assertEqual(controller.manual, Mode.AUTO)
        self.assertTrue(controller.tent_prompt)
        self.assertFalse(controller.stand_prompt)
        self.assertEqual(controller.target, Target())
        self.assertEqual(controller.target.mode, "unavailable")
        self.assertEqual(controller.generation, 0)
        self.assertIsNone(controller.applied)
        self.assertIsNone(controller.error_code)
        self.assertIsNone(controller.error_detail)
        self.assertIsNone(controller.error)
        self.assertIsNone(controller.hardware)
        self.assertIsNone(controller.pending_notification)
        self.assertEqual(controller.policy.mode, MODE_DISABLED)

    def test_policy_injection(self):
        policy = Policy(PolicyConfig(stale_seconds=0.5))
        controller = Controller(policy=policy)
        self.assertIs(controller.policy, policy)
        self.assertIs(controller.config, policy.config)
        with self.assertRaises(ValueError):
            Controller(policy=policy, config=PolicyConfig())

    def test_config_injection(self):
        config = PolicyConfig(enter_seconds=1.5)
        controller = Controller(config=config)
        self.assertIs(controller.config, config)
        self.assertIs(controller.policy.config, config)


class ManualModeTests(ControllerTestCase):
    def test_hardware_overrides_every_manual_mode(self):
        for mode in (Mode.LAPTOP, Mode.TENT, Mode.STAND, Mode.TABLET):
            with self.subTest(mode=mode):
                controller = Controller()
                controller.set_manual(mode)
                controller.update(1.0, sample(0.0, stamp=1.0), True)
                target = controller.target
                self.assertEqual(target.mode, "hardware")
                self.assertTrue(target.tablet)
                self.assertIsNone(target.rotation)
                self.assertFalse(target.disable_internal_input)
                self.assertEqual(controller.manual, Mode.AUTO)

    def test_manual_targets(self):
        cases = {
            Mode.LAPTOP: ("laptop", False, 0, False),
            Mode.TENT: ("tent", True, 180, True),
            Mode.STAND: ("stand", True, 0, True),
            Mode.TABLET: ("tablet", True, None, True),
        }
        for mode, expected in cases.items():
            with self.subTest(mode=mode):
                controller = Controller()
                controller.set_manual(mode)
                generation, target = controller.update(
                    1.0, sample(0.0, stamp=1.0), False
                )
                self.assertEqual(
                    (
                        target.mode,
                        target.tablet,
                        target.rotation,
                        target.disable_internal_input,
                    ),
                    expected,
                )
                self.assertEqual(target, controller.target)
                self.assertEqual(generation, controller.generation)
                self.assertIsNone(controller.applied)

    def test_manual_accepts_strings(self):
        self.controller.set_manual("stand")
        self.assertEqual(self.controller.manual, Mode.STAND)
        self.controller.set_manual("auto")
        self.assertEqual(self.controller.manual, Mode.AUTO)

    def test_invalid_mode_rejected_with_stable_code(self):
        with self.assertRaises(ValueError) as caught:
            self.controller.set_manual("shell-command")
        self.assertEqual(str(caught.exception), ErrorCode.INVALID_MODE)

    def test_hardware_prevents_manual_override(self):
        self.controller.update(1.0, sample(0.0, stamp=1.0), True)
        with self.assertRaises(ValueError) as caught:
            self.controller.set_manual(Mode.LAPTOP)
        self.assertEqual(str(caught.exception), ErrorCode.HARDWARE_PRIORITY)
        # auto is always allowed, even while folded.
        self.controller.set_manual(Mode.AUTO)

    def test_manual_selection_resets_when_hardware_takes_over(self):
        self.controller.set_manual(Mode.STAND)
        self.controller.update(1.0, sample(0.0, stamp=1.0), True)
        self.assertEqual(self.controller.manual, Mode.AUTO)


class AutomaticModeTests(ControllerTestCase):
    def test_tent_prompt_is_not_a_target_until_confirmed(self):
        self.hold(80.0)
        self.assertEqual(self.controller.policy.mode, MODE_TENT_PROMPT)
        self.assertEqual(self.controller.target.mode, "laptop")
        epoch = self.controller.policy.epoch
        self.assertEqual(self.controller.pending_notification, epoch)
        self.assertTrue(self.controller.confirm_tent(epoch))
        _, target = self.step(80.0)
        self.assertEqual(target.mode, "tent")
        self.assertTrue(target.tablet)
        self.assertEqual(target.rotation, 180)
        self.assertTrue(target.disable_internal_input)
        self.assertEqual(self.controller.policy.mode, "T")

    def test_auto_stand_target(self):
        self.hold(175.0)
        self.assertEqual(self.controller.target.mode, "stand")
        self.assertTrue(self.controller.target.tablet)
        self.assertEqual(self.controller.target.rotation, 0)
        self.assertTrue(self.controller.target.disable_internal_input)

    def test_stand_prompt_waits_for_confirmation(self):
        self.controller.stand_prompt = True
        self.hold(175.0)
        self.assertEqual(self.controller.policy.mode, MODE_STAND_PROMPT)
        self.assertEqual(self.controller.target.mode, "laptop")
        self.assertEqual(self.controller.pending_prompt, "stand")
        epoch = self.controller.policy.epoch
        self.assertEqual(self.controller.pending_notification, epoch)
        self.assertFalse(self.controller.confirm_tent(epoch))
        self.assertTrue(self.controller.confirm_stand(epoch))
        self.step(175.0)
        self.assertEqual(self.controller.target.mode, "stand")
        self.assertTrue(self.controller.target.tablet)
        self.assertIsNone(self.controller.pending_prompt)

    def test_disabling_stand_prompt_applies_stand_on_next_update(self):
        self.controller.stand_prompt = True
        self.hold(175.0)
        self.assertEqual(self.controller.policy.mode, MODE_STAND_PROMPT)
        self.controller.stand_prompt = False
        self.step(175.0)
        self.assertEqual(self.controller.policy.mode, "S")
        self.assertEqual(self.controller.target.mode, "stand")
        self.assertIsNone(self.controller.pending_notification)

    def test_manual_stand_enters_stand_without_prompt(self):
        self.controller.stand_prompt = True
        self.controller.set_manual(Mode.STAND)
        self.hold(175.0)
        self.assertEqual(self.controller.policy.mode, MODE_STAND)
        self.assertEqual(self.controller.target.mode, "stand")
        self.assertIsNone(self.controller.pending_prompt)
        self.assertFalse(self.controller.confirm_stand(self.controller.policy.epoch))

    def test_stand_to_tent_switches_without_confirmation(self):
        self.hold(175.0)
        self.assertEqual(self.controller.target.mode, "stand")
        self.hold(80.0)
        self.assertEqual(self.controller.policy.mode, "T")
        self.assertEqual(self.controller.target.mode, "tent")
        self.assertIsNone(self.controller.pending_notification)

    def test_manual_non_laptop_modes_return_to_tent_without_prompt(self):
        for mode in (Mode.TENT, Mode.STAND, Mode.TABLET):
            with self.subTest(mode=mode):
                controller = Controller()
                controller.set_manual(mode)
                now = 0.0
                for _ in range(30):
                    now += 0.1
                    controller.update(now, sample(80.0, stamp=now), False)

                self.assertEqual(controller.policy.mode, MODE_TENT)
                controller.set_manual(Mode.AUTO)
                now += 0.1
                _, target = controller.update(now, sample(80.0, stamp=now), False)
                self.assertEqual(target.mode, "tent")
                self.assertIsNone(controller.pending_prompt)
                self.assertIsNone(controller.pending_notification)

    def test_manual_laptop_origin_still_prompts_for_tent(self):
        self.controller.set_manual(Mode.LAPTOP)
        self.hold(80.0)
        self.assertEqual(self.controller.policy.mode, MODE_TENT_PROMPT)

        self.controller.set_manual(Mode.AUTO)
        self.step(80.0)
        self.assertEqual(self.controller.policy.mode, MODE_TENT_PROMPT)
        self.assertEqual(self.controller.pending_prompt, "tent")
        self.assertIsNotNone(self.controller.pending_notification)

    def test_auto_returns_to_laptop(self):
        self.hold(175.0)
        self.assertEqual(self.controller.target.mode, "stand")
        self.hold(0.0)
        self.assertEqual(self.controller.target.mode, "laptop")
        self.assertFalse(self.controller.target.tablet)
        self.assertEqual(self.controller.target.rotation, 0)

    def test_hardware_resumes_auto_after_unfold(self):
        self.hold(180.0, hardware=True)
        self.assertEqual(self.controller.target.mode, "hardware")
        self.assertEqual(self.controller.manual, Mode.AUTO)
        self.hold(0.0, hardware=False)
        self.assertEqual(self.controller.target.mode, "laptop")

    def test_prompt_disabled_enters_tent_without_confirmation(self):
        self.controller.tent_prompt = False
        _, target = self.hold(80.0)
        self.assertEqual(self.controller.policy.mode, MODE_TENT)
        self.assertIsNone(self.controller.pending_notification)
        self.assertFalse(self.controller.confirm_tent(self.controller.policy.epoch))
        self.assertEqual(target.mode, "tent")

    def test_disabling_tent_prompt_resolves_pending_prompt(self):
        self.hold(80.0)
        self.assertEqual(self.controller.policy.mode, MODE_TENT_PROMPT)
        self.controller.tent_prompt = False
        _, target = self.hold(80.0)
        self.assertEqual(self.controller.policy.mode, MODE_TENT)
        self.assertIsNone(self.controller.pending_prompt)
        self.assertEqual(target.mode, "tent")

    def test_manual_tent_enters_tent_without_prompt(self):
        self.controller.set_manual(Mode.TENT)
        self.hold(80.0)
        self.assertEqual(self.controller.policy.mode, MODE_TENT)
        self.assertIsNone(self.controller.pending_notification)
        self.assertFalse(self.controller.confirm_tent(self.controller.policy.epoch))

    def test_confirm_requires_current_epoch(self):
        self.hold(80.0)
        epoch = self.controller.policy.epoch
        self.assertFalse(self.controller.confirm_tent(epoch + 1))
        self.assertFalse(self.controller.confirm_tent(epoch - 1))
        self.assertTrue(self.controller.confirm_tent(epoch))


class UnknownHardwareTests(ControllerTestCase):
    def test_unknown_hardware_releases_software_takeover(self):
        self.controller.set_manual(Mode.TABLET)
        _, target = self.step(0.0, hardware=None)
        self.assertEqual(target.mode, "unavailable")
        self.assertIsNone(target.tablet)
        self.assertIsNone(target.rotation)
        self.assertFalse(target.disable_internal_input)

    def test_manual_mode_survives_transient_unknown(self):
        self.controller.set_manual(Mode.TENT)
        self.step(0.0, hardware=None)
        self.assertEqual(self.controller.manual, Mode.TENT)
        _, target = self.step(0.0, hardware=False)
        self.assertEqual(target.mode, "tent")

    def test_stale_sensor_releases_manual_mode(self):
        self.controller.set_manual(Mode.TENT)
        _, target = self.step(0.0)
        self.assertEqual(target.mode, "tent")
        self.now += self.controller.config.stale_seconds + 0.1
        _, target = self.controller.update(self.now, SensorReading(), False)
        self.assertEqual(target.mode, "unavailable")
        self.assertIsNone(target.tablet)
        self.assertEqual(self.controller.policy.mode, MODE_DISABLED)

    def test_invalid_hardware_type_rejected(self):
        with self.assertRaises(ValueError):
            self.controller.update(1.0, sample(0.0, stamp=1.0), "folded")


class GenerationTransactionTests(ControllerTestCase):
    def test_unchanged_target_does_not_bump_generation(self):
        generation, target = self.step(0.0)
        self.assertEqual(target.mode, "laptop")
        self.step(0.0)
        self.assertEqual(self.controller.generation, generation)

    def test_set_manual_does_not_bump_generation(self):
        generation, _ = self.step(0.0)
        self.controller.set_manual(Mode.TENT)
        self.assertEqual(self.controller.generation, generation)
        new_generation, target = self.step(0.0)
        self.assertEqual(target.mode, "tent")
        self.assertEqual(new_generation, generation + 1)

    def test_target_change_drops_previous_acknowledgement(self):
        generation, target = self.step(0.0)
        self.assertTrue(self.controller.acknowledge(generation, True))
        self.assertEqual(self.controller.applied, target)
        self.controller.set_manual(Mode.STAND)
        new_generation, _ = self.step(0.0)
        self.assertGreater(new_generation, generation)
        self.assertIsNone(self.controller.applied)
        self.assertFalse(self.controller.acknowledge(generation, True))

    def test_stale_acknowledgement_is_ignored(self):
        old_generation, _ = self.step(0.0)
        self.controller.set_manual(Mode.TENT)
        new_generation, target = self.step(0.0)
        self.assertGreater(new_generation, old_generation)
        self.assertFalse(self.controller.acknowledge(old_generation, True))
        self.assertIsNone(self.controller.applied)
        self.assertTrue(self.controller.acknowledge(new_generation, True))
        self.assertEqual(self.controller.applied, target)

    def test_failure_does_not_claim_success(self):
        generation, _ = self.step(0.0)
        self.assertTrue(
            self.controller.acknowledge(
                generation, False, ErrorCode.RELAY_EXITED, "exit=5"
            )
        )
        self.assertIsNone(self.controller.applied)
        self.assertEqual(self.controller.error_code, ErrorCode.RELAY_EXITED)
        self.assertEqual(self.controller.error_detail, "exit=5")
        self.assertEqual(self.controller.error, ErrorCode.RELAY_EXITED)

    def test_failure_defaults_to_internal_error(self):
        generation, _ = self.step(0.0)
        self.assertTrue(self.controller.acknowledge(generation, False))
        self.assertIsNone(self.controller.applied)
        self.assertEqual(self.controller.error_code, ErrorCode.INTERNAL)
        self.assertIsNone(self.controller.error_detail)

    def test_success_clears_error(self):
        generation, _ = self.step(0.0)
        self.controller.acknowledge(generation, False, ErrorCode.RELAY_START)
        self.assertEqual(self.controller.error_code, ErrorCode.RELAY_START)
        self.controller.acknowledge(generation, True)
        self.assertIsNone(self.controller.error_code)
        self.assertIsNone(self.controller.error_detail)

    def test_error_alias_is_writable(self):
        self.controller.error = ErrorCode.SENSOR_TIMEOUT
        self.assertEqual(self.controller.error_code, ErrorCode.SENSOR_TIMEOUT)
        self.controller.error = None
        self.assertIsNone(self.controller.error_code)
        self.assertIsNone(self.controller.error_detail)

    def test_target_change_clears_error(self):
        generation, _ = self.step(0.0)
        self.controller.acknowledge(generation, False, ErrorCode.RELAY_EXITED)
        self.assertEqual(self.controller.error_code, ErrorCode.RELAY_EXITED)
        self.step(0.0, hardware=True)
        self.assertEqual(self.controller.target.mode, "hardware")
        self.assertIsNone(self.controller.error_code)
        self.assertIsNone(self.controller.error_detail)


class SuspendResetTests(ControllerTestCase):
    def test_reset_after_suspend_resets_transaction(self):
        self.hold(80.0)
        epoch = self.controller.policy.epoch
        self.assertTrue(self.controller.confirm_tent(epoch))
        generation, target = self.step(80.0)
        self.assertEqual(target.mode, "tent")
        self.assertTrue(self.controller.acknowledge(generation, True))
        self.assertIsNotNone(self.controller.applied)

        self.controller.reset_after_suspend()

        self.assertEqual(self.controller.manual, Mode.AUTO)
        self.assertEqual(self.controller.policy.mode, MODE_DISABLED)
        self.assertEqual(self.controller.generation, generation + 1)
        self.assertIsNone(self.controller.applied)
        self.assertEqual(self.controller.target, Target())
        self.assertIsNone(self.controller.error_code)
        self.assertIsNone(self.controller.hardware)
        self.assertEqual(len(self.controller.policy.history), 0)
        # Acknowledgements from before the sleep are transactional history.
        self.assertFalse(self.controller.acknowledge(generation, True))

    def test_reset_after_suspend_keeps_hardware_priority_on_resume(self):
        self.controller.update(1.0, sample(0.0, stamp=1.0), True)
        self.assertEqual(self.controller.target.mode, "hardware")
        self.controller.reset_after_suspend()
        self.assertEqual(self.controller.target.mode, "unavailable")
        self.controller.update(2.0, sample(0.0, stamp=2.0), True)
        self.assertEqual(self.controller.target.mode, "hardware")


class PromptNotificationTests(ControllerTestCase):
    def test_pending_notification_tracks_policy_epoch(self):
        self.hold(80.0)
        self.assertEqual(
            self.controller.pending_notification,
            self.controller.policy.epoch,
        )
        self.assertIsInstance(self.controller.pending_notification, int)

    def test_pending_notification_ignores_prompt_modes(self):
        self.hold(0.0)
        self.assertIsNone(self.controller.pending_notification)
        self.hold(175.0)
        self.assertIsNone(self.controller.pending_notification)

    def test_pending_notification_disabled_by_tent_prompt(self):
        self.hold(80.0)
        self.assertIsNotNone(self.controller.pending_notification)
        self.controller.tent_prompt = False
        self.assertIsNone(self.controller.pending_notification)


if __name__ == "__main__":
    unittest.main()
