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
)
from .protocol import ErrorCode

# Automatic policy mode -> public software target mode.
AUTO_TARGET_MODES = {
    MODE_TENT: "tent",
    MODE_STAND: "stand",
}

# Public target modes (``model.Target.mode``) for the software takeover.
TARGET_LAPTOP = "laptop"
TARGET_TENT = "tent"
TARGET_STAND = "stand"
TARGET_TABLET = "tablet"
TARGET_HARDWARE = "hardware"
TARGET_UNAVAILABLE = "unavailable"


class Controller:
    """Pure posture/target decision layer (no devices, no D-Bus, no clock)."""

    def __init__(
        self,
        policy: Policy | None = None,
        config: PolicyConfig | None = None,
    ) -> None:
        if policy is not None and config is not None:
            raise ValueError("pass either policy or config, not both")
        self.policy = policy if policy is not None else Policy(config)
        self.config = self.policy.config
        self.manual = Mode.AUTO
        self.tent_prompt = True
        self.stand_prompt = False
        self.target = Target()
        self.generation = 0
        self.applied: Target | None = None
        self.error_code: str | None = None
        self.error_detail: str | None = None
        self.hardware: bool | None = None

    # -- compatibility -------------------------------------------------------

    @property
    def error(self) -> str | None:
        """Compatibility alias for :attr:`error_code` (old 0.2.x field)."""

        return self.error_code

    @error.setter
    def error(self, value: str | None) -> None:
        self.error_code = value
        self.error_detail = None

    # -- derived state -------------------------------------------------------

    @property
    def pending_notification(self) -> int | None:
        """Epoch of the posture prompt the user should see right now, if any.

        Same rule as 0.2.x: prompt only in automatic mode, when the tent
        or stand prompt setting is enabled and when the policy is waiting for
        the corresponding confirmation.
        """

        return self.policy.epoch if self.pending_prompt is not None else None

    @property
    def pending_prompt(self) -> str | None:
        """Kind of posture prompt the user should see right now, if any."""

        if self.manual != Mode.AUTO:
            return None
        prompt = self.policy.pending_prompt
        if prompt == "tent" and not self.tent_prompt:
            return None
        if prompt == "stand" and not self.stand_prompt:
            return None
        return prompt

    # -- public actions ------------------------------------------------------

    def set_manual(self, mode: Mode | str) -> None:
        """Select a manual mode; raises ``ValueError`` with a stable code.

        The caller (service/engine layer) owns the explicit generation bump
        for ``SetMode``; this method only changes the decision input.
        """

        try:
            normalized = mode if isinstance(mode, Mode) else Mode(str(mode))
        except ValueError:
            raise ValueError(ErrorCode.INVALID_MODE) from None
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

        self.policy.reset_after_suspend()
        self.manual = Mode.AUTO
        self.hardware = None
        self.generation += 1
        self.applied = None
        self.error_code = None
        self.error_detail = None
        self.target = Target()

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
        self.hardware = hardware
        # Manual tent, stand and tablet selections are explicit non-laptop
        # origins.  Only automatic or manual laptop operation may prompt on the
        # next change, otherwise switching back to auto would resurrect a stale
        # prompt for a transition the user already made explicitly.
        prompt_from_laptop = self.manual in (Mode.AUTO, Mode.LAPTOP)
        self.policy.update(
            now,
            reading,
            hardware,
            tent_prompt=bool(self.tent_prompt) and prompt_from_laptop,
            stand_prompt=bool(self.stand_prompt) and prompt_from_laptop,
        )

        if hardware is True:
            target = Target(
                mode=TARGET_HARDWARE,
                tablet=True,
                rotation=None,
                disable_internal_input=False,
            )
        elif hardware is None:
            target = Target()
        elif self.policy.mode == MODE_DISABLED:
            # Stale/invalid sensor data releases the software takeover even in
            # manual mode: without sensor evidence we cannot keep a fake
            # tablet posture.
            target = Target()
        else:
            target = self._software_target()

        if target != self.target:
            self.generation += 1
            self.target = target
            self.applied = None
            self.error_code = None
            self.error_detail = None
        return self.generation, self.target

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
                disable_internal_input=True,
            )
        if mode == TARGET_STAND:
            return Target(
                mode=TARGET_STAND,
                tablet=True,
                rotation=0,
                disable_internal_input=True,
            )
        # Manual tablet mode: keep native rotation policy, disable the
        # internal input devices.
        return Target(
            mode=TARGET_TABLET,
            tablet=True,
            rotation=None,
            disable_internal_input=True,
        )
