"""Session-independent posture decisions.

The controller owns the pure :class:`~panasonic_posture.policy.Policy` and
translates the policy state into the public :class:`~panasonic_posture.model.Target`
the service layer asks the backend to apply.  It also implements the
generation/acknowledgement transaction: a target is *not* applied until a
matching acknowledgement arrives, so the session adapter can never display a
mode the backend did not actually adopt.

The module performs no I/O and reads no clock; callers pass ``now`` and the
latest :class:`~panasonic_posture.model.SensorReading`.
"""

from __future__ import annotations

from .model import Mode, SensorReading, Target
from .policy import (
    MODE_DISABLED,
    MODE_STAND,
    MODE_TENT,
    Policy,
    PolicyConfig,
    in_tent_zone,
)
from .contracts import ErrorCode

# Automatic policy mode -> public software target mode.
AUTO_TARGET_MODES = {
    MODE_TENT: "tent",
    MODE_STAND: "stand",
}

# Public target modes (``model.Target.mode``) for the software takeover.
TARGET_LAPTOP = "laptop"
TARGET_TENT = "tent"
TARGET_STAND = "stand"
TARGET_HARDWARE = "hardware"
TARGET_UNAVAILABLE = "unavailable"


class Controller:
    """Pure posture/target decision layer (no devices, no D-Bus, no clock)."""

    def __init__(
        self,
        policy: Policy | None = None,
        config: PolicyConfig | None = None,
        *,
        input_control: bool = True,
    ) -> None:
        if policy is not None and config is not None:
            raise ValueError("pass either policy or config, not both")
        if type(input_control) is not bool:
            raise ValueError("input_control must be a boolean")
        self._input_control = input_control
        self.policy = policy if policy is not None else Policy(config)
        self.config = self.policy.config
        self.manual = Mode.AUTO
        self.tent_enabled = True
        self.tent_prompt = True
        self.stand_prompt = False
        self._unattended = False
        self.target = Target()
        self.generation = 0
        self.applied: Target | None = None
        self.error_code: str | None = None
        self.error_detail: str | None = None
        self.hardware: bool | None = None
        self._release_pose: str | None = None
        self._release_candidate: str | None = None
        self._release_since: float | None = None

    # -- derived state -------------------------------------------------------

    @property
    def policy_mode(self) -> str:
        return self.policy.mode

    @property
    def sensor_fresh(self) -> bool:
        return self.policy.fresh

    @property
    def sensor_stable(self) -> bool:
        return self.policy.stable

    @property
    def posture_pending(self) -> bool:
        return self.policy.posture_pending

    @property
    def hardware_release_pose(self) -> str | None:
        """Recognize an opening posture while the physical switch still says tablet."""

        return self._release_pose if self.tent_enabled else None

    @property
    def pending_notification(self) -> int | None:
        """Epoch of the posture prompt the user should see right now, if any.

        Same rule as 0.2.x: prompt only in automatic mode, when the tent
        or stand prompt setting is enabled and when the policy is waiting for
        the corresponding confirmation.  The prompt must also be confirmable
        with the current sensor evidence; exposing an action that the service
        would reject causes the notification to be retried indefinitely.
        """

        return self.policy.epoch if self.pending_prompt is not None else None

    @property
    def pending_prompt(self) -> str | None:
        """Kind of actionable posture prompt the user should see now, if any."""

        if self.manual != Mode.AUTO:
            return None
        prompt = self.policy.pending_prompt
        if prompt is None or not self.policy.confirmation_ready:
            return None
        if prompt == "tent" and (not self.tent_enabled or not self.tent_prompt):
            return None
        if prompt == "stand" and not self.stand_prompt:
            return None
        return prompt

    # -- public actions ------------------------------------------------------

    def set_session_context(self, unattended: bool, *, continuing: bool = False) -> None:
        """Accept host-validated context and settle once on the same session's wake.

        Lease loss and a different owner clear context without adopting a new
        baseline. Only the host can establish session continuity.
        """
        if self._unattended and not unattended and continuing:
            self.policy.adopt_next_stable_pose()
        self._unattended = unattended

    def reset_after_calibration(self) -> None:
        """Discard old geometry while retaining normal confirmation rules."""
        self.policy.reset_after_suspend(adopt_first_pose=False)

    def set_manual(self, mode: Mode | str) -> None:
        """Select a manual mode; raises ``ValueError`` with a stable code.

        The caller (service/engine layer) owns the explicit generation bump
        for ``SetMode``; this method only changes the decision input.
        """

        try:
            normalized = mode if isinstance(mode, Mode) else Mode(str(mode))
        except ValueError:
            raise ValueError(ErrorCode.INVALID_MODE) from None
        if normalized == Mode.TABLET:
            # Retained only to decode older snapshots. Manual tablet selection
            # is intentionally unavailable on every platform.
            raise ValueError(ErrorCode.INVALID_MODE)
        if self.hardware is True and normalized != Mode.AUTO:
            raise ValueError(ErrorCode.HARDWARE_PRIORITY)
        self.manual = normalized

    def confirm_tent(self, epoch: int) -> bool:
        """Accept the tent prompt when every precondition still holds."""

        if self.pending_prompt != "tent":
            return False
        return self.policy.confirm(epoch)

    def confirm_stand(self, epoch: int) -> bool:
        """Accept the stand prompt when every precondition still holds."""

        if self.pending_prompt != "stand":
            return False
        return self.policy.confirm_stand(epoch)

    def acknowledge(
        self,
        generation: int,
        success: bool,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        """Report a backend transaction result.

        Stale acknowledgements (from a previous generation) are ignored so a
        slow backend can never claim success for an outdated target.
        """

        if generation != self.generation:
            return False
        if success:
            self.applied = self.target
            self.error_code = None
            self.error_detail = None
        else:
            self.applied = None
            self.error_code = error_code or ErrorCode.INTERNAL
            self.error_detail = error_detail
        return True

    def reset_after_suspend(self) -> None:
        """Reset the whole decision transaction after a suspend/resume cycle.

        Called once per ``PrepareForSleep`` event by the service layer.  It
        clears the policy anchors (P2-7), drops the manual selection, bumps
        the generation (invalidating session acknowledgements) and releases
        the target until fresh evidence is available.
        """

        self._unattended = False
        self.policy.reset_after_suspend()
        self.manual = Mode.AUTO
        self.hardware = None
        self.generation += 1
        self.applied = None
        self.error_code = None
        self.error_detail = None
        self.target = Target()
        self._release_pose = None
        self._release_candidate = None
        self._release_since = None

    # -- state machine -------------------------------------------------------

    def update(
        self,
        now: float,
        reading: SensorReading,
        hardware: bool | None,
    ) -> tuple[int, Target]:
        """Advance the decision state; return ``(generation, target)``."""

        if hardware is not None and not isinstance(hardware, bool):
            raise ValueError("hardware must be True, False or None")
        if hardware is True and self.hardware is not True:
            # The physical switch has priority over every manual selection.
            self.manual = Mode.AUTO
            self._release_pose = None
            self._release_candidate = None
            self._release_since = None
        self.hardware = hardware
        prompt_from_laptop = not self._unattended and self.manual in (Mode.AUTO, Mode.LAPTOP)
        self.policy.update(
            now,
            reading,
            hardware,
            tent_enabled=bool(self.tent_enabled),
            tent_prompt=bool(self.tent_prompt) and prompt_from_laptop,
            stand_prompt=bool(self.stand_prompt) and prompt_from_laptop,
        )
        self._advance_release_candidate(now)

        if hardware is True:
            target = Target(
                mode=TARGET_HARDWARE,
                tablet=True,
                rotation=None,
                disable_internal_input=False,
            )
        elif hardware is None:
            target = Target()
        elif self.policy.posture_pending and self._release_pose == TARGET_TENT:
            # The geometry reached the tent zone before the switch released.
            # Apply that target on the first released tick instead of waiting
            # through another settle window exposed as laptop orientation.
            target = Target(
                mode=TARGET_TENT,
                tablet=True,
                rotation=180,
                disable_internal_input=self._input_control,
            )
        elif self.policy.mode == MODE_DISABLED:
            # Stale/invalid sensor data releases the software takeover even in
            # manual mode: without sensor evidence we cannot keep a fake
            # tablet posture.
            target = Target()
        else:
            target = self._software_target()
        if hardware is False and self.policy.mode in (MODE_TENT, MODE_STAND):
            self._release_pose = None
            self._release_candidate = None
            self._release_since = None

        if target != self.target:
            self.generation += 1
            self.target = target
            self.applied = None
            self.error_code = None
            self.error_detail = None
        return self.generation, self.target

    def _advance_release_candidate(self, now: float) -> None:
        if self.hardware is not True:
            if self.hardware is False and self.policy.posture_pending:
                return
            self._release_pose = None
            self._release_candidate = None
            self._release_since = None
            return
        pitch, roll, _flat = self.policy.current_geometry
        candidate = TARGET_TENT if self.tent_enabled and in_tent_zone(pitch, roll) else None
        if candidate is None:
            self._release_pose = None
            self._release_candidate = None
            self._release_since = None
            return
        if candidate != self._release_candidate:
            self._release_pose = None
            self._release_candidate = candidate
            self._release_since = now
            return
        if self._release_since is not None and now - self._release_since >= 0.3:
            self._release_pose = candidate

    # -- internals -----------------------------------------------------------

    def _software_target(self) -> Target:
        if self.manual == Mode.AUTO:
            mode = AUTO_TARGET_MODES.get(self.policy.mode, TARGET_LAPTOP)
        else:
            mode = str(self.manual)
        if mode == TARGET_LAPTOP:
            return Target(
                mode=TARGET_LAPTOP,
                tablet=False,
                rotation=0,
                disable_internal_input=False,
            )
        if mode == TARGET_TENT:
            return Target(
                mode=TARGET_TENT,
                tablet=True,
                rotation=180,
                disable_internal_input=self._input_control,
            )
        if mode == TARGET_STAND:
            return Target(
                mode=TARGET_STAND,
                tablet=True,
                rotation=0,
                disable_internal_input=self._input_control,
            )
        return Target(
            mode=TARGET_LAPTOP,
            tablet=False,
            rotation=0,
            disable_internal_input=False,
        )
