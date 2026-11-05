"""Pure posture state machine for the Panasonic posture manager.

The module is deterministic and free of side effects: callers pass the current
time and the latest :class:`~panasonic_posture.model.SensorReading`, then read
the resulting mode.  No clocks, files, threads or D-Bus access live here.

Internal modes
--------------

``L`` laptop
    Normal laptop posture; no software takeover.
``P`` tent prompt
    The device looks like a tent; wait for the user to confirm.
``Q`` stand prompt
    The device looks like it is in stand posture; wait for the user to
    confirm when the optional stand prompt is enabled.
``T`` tent
    Tent posture confirmed (software tablet, rotated 180 degrees).
``S`` stand
    Stand posture (software tablet, rotation 0).
``H`` hardware
    The physical tablet switch is ON; hardware always wins.
``D`` disabled
    No trustworthy decision: sensor data is stale/invalid or the hardware
    switch state is unknown.

Only pure decisions live in this module; applying a target to devices is the
controller/service layer's job.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum, auto

from .model import SensorReading

# --- Internal modes ---------------------------------------------------------

MODE_LAPTOP = "L"
MODE_TENT_PROMPT = "P"
MODE_STAND_PROMPT = "Q"
MODE_TENT = "T"
MODE_STAND = "S"
MODE_HARDWARE = "H"
MODE_DISABLED = "D"

POLICY_MODES = (
    MODE_LAPTOP,
    MODE_TENT_PROMPT,
    MODE_STAND_PROMPT,
    MODE_TENT,
    MODE_STAND,
    MODE_HARDWARE,
    MODE_DISABLED,
)

# Gravity magnitude acceptance window for healthy 1 g sensors.  Samples
# outside it (free fall, shocks, wrong scale, bad mount) are invalid and never
# update the validity anchors.
MIN_MAGNITUDE = 7.5
MAX_MAGNITUDE = 12.0

# Tent confirmation zone (public "tent" definition).  ``confirm()`` requires
# the *current* geometry to still be inside this zone.
TENT_MIN_PITCH = 65.0
TENT_MAX_PITCH = 115.0
TENT_MAX_ROLL = 25.0

# Hysteresis bands: once in P/Q/T/S the posture is held until the device leaves
# a wider band for ``leave_seconds``.
TENT_HOLD_MIN_PITCH = 45.0
TENT_HOLD_MAX_PITCH = 155.0
TENT_HOLD_MAX_ROLL = 40.0
STAND_MIN_FLAT = 155.0
# The stand pose is the widest hysteresis band because the sensor is sampled
# at only ~2 Hz in shared mode.  A 15 degree hold band and the generic 0.7 s
# leave timer let one or two noisy samples release the relay and the display
# transaction, which immediately re-enters stand and repeats the cycle.
STAND_HOLD_MIN_FLAT = 130.0
# A stable pose flatter than the tent hold band is a real laptop pose.  A
# non-laptop origin survives intermediate hinge angles, but must not survive
# the user settling back into laptop form.
LAPTOP_MAX_FLAT = TENT_HOLD_MIN_PITCH

# Consecutive unit vectors within this angle are considered a stable run.
SETTLE_MAX_ANGLE_DEGREES = 8.0
SETTLE_MAX_COS = math.cos(math.radians(SETTLE_MAX_ANGLE_DEGREES))


@dataclass(frozen=True)
class PolicyConfig:
    """All posture timing constants in one place (seconds unless stated).

    ``history_max_samples`` is a hard upper bound on top of the time window so
    a pathological timestamp stream can never grow the smoothing history
    without limit (review P1-3).
    """

    enter_seconds: float = 0.6
    leave_seconds: float = 0.7
    # Stand mode has its own, longer exit debounce.  It must outlast the
    # IIO/sysfs source hand-off so a source change cannot look like a posture
    # change and restart the whole takeover.
    stand_leave_seconds: float = 2.0
    # A confirmed tent mode may be restored without another prompt when the
    # device briefly leaves and re-enters the tent zone (for example while the
    # user adjusts the hinge).  A settled laptop pose cancels this grace;
    # a stale exit never skips confirmation either.
    tent_reentry_seconds: float = 5.0
    # The IIO/sysfs hand-off on this sensor hub can take more than two
    # seconds.  Releasing a software takeover on the first short gap makes the
    # relay stop and start again, which looks like the posture itself is
    # alternating between stand and laptop.  Keep the last decision through a
    # full hand-off, but still release it when the sensor is genuinely gone.
    stale_seconds: float = 5.0
    history_seconds: float = 0.75
    stable_seconds: float = 0.45
    history_max_samples: int = 128

    def __post_init__(self) -> None:
        for name in (
            "enter_seconds",
            "leave_seconds",
            "stand_leave_seconds",
            "tent_reentry_seconds",
            "stale_seconds",
            "history_seconds",
            "stable_seconds",
        ):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if self.history_max_samples < 1:
            raise ValueError("history_max_samples must be at least 1")


def geometry(
    vector: Sequence[float] | None,
) -> tuple[
    tuple[float, float, float], float, float | None, float, float
]:
    """Return ``(unit, magnitude, pitch, roll, flat)`` for an acceleration vector.

    *pitch* is ``None`` when the device is (almost) sideways, matching the
    historical definition.  Invalid input raises :class:`ValueError`; callers
    must never let malformed sensor data crash the state machine silently.
    """

    if vector is None:
        raise ValueError("invalid accelerometer sample")
    try:
        values = tuple(float(component) for component in vector)
    except (TypeError, ValueError):
        raise ValueError("invalid accelerometer sample") from None
    if len(values) != 3 or not all(math.isfinite(component) for component in values):
        raise ValueError("invalid accelerometer sample")
    x, y, z = values
    magnitude = math.sqrt(x * x + y * y + z * z)
    if magnitude < 1e-6:
        raise ValueError("no gravity direction")
    unit = (x / magnitude, y / magnitude, z / magnitude)
    unit_x, unit_y, unit_z = unit
    horizontal = math.hypot(unit_y, unit_z)
    pitch = (
        math.degrees(math.atan2(-unit_y, -unit_z))
        if horizontal >= 0.05
        else None
    )
    roll = math.degrees(math.atan2(unit_x, horizontal))
    flat = math.degrees(math.acos(max(-1.0, min(1.0, -unit_z))))
    return unit, magnitude, pitch, roll, flat


def in_tent_zone(pitch: float | None, roll: float) -> bool:
    """True when ``(pitch, roll)`` lies in the documented tent zone."""

    return (
        pitch is not None
        and TENT_MIN_PITCH <= pitch <= TENT_MAX_PITCH
        and abs(roll) <= TENT_MAX_ROLL
    )


def in_stand_zone(flat: float | None) -> bool:
    """True when ``flat`` lies in the documented stand zone."""

    return flat is not None and flat >= STAND_MIN_FLAT


class _Origin(Enum):
    """Where the current posture sequence came from."""

    UNKNOWN = auto()
    LAPTOP = auto()
    NON_LAPTOP = auto()


@dataclass
class _SourceState:
    """Single source-of-truth for prompt and direct-entry decisions."""

    origin: _Origin = _Origin.UNKNOWN
    startup_pending: bool = True
    hardware_release_pending: bool = False
    tent_exit_at: float | None = None


class Policy:
    """Posture state machine fed by sensor samples and the hardware switch.

    The public contract is small: call :meth:`update` for every sample, read
    :attr:`mode`/``stable``, call :meth:`confirm`/:meth:`confirm_stand` for the
    posture prompts and
    :meth:`reset_after_suspend` after the machine wakes up.
    """

    def __init__(self, config: PolicyConfig | None = None) -> None:
        self.config = config if config is not None else PolicyConfig()
        self.mode = MODE_DISABLED
        self.epoch = 0
        self.stable = False
        self.fresh = False
        self.hardware: bool | None = None
        # Smoothed samples kept for ``history_seconds``; bounded (P1-3).
        self.history: deque[
            tuple[float, tuple[float, float, float]]
        ] = deque()
        # Anchors of the last *valid* sample.  Invalid samples never touch
        # these (review P2-5).
        self.last_stamp: float | None = None
        self.last_geometry: (
            tuple[tuple[float, float, float], float, float | None, float, float]
            | None
        ) = None
        self.last_sample: (
            tuple[float, tuple[float, float, float]] | None
        ) = None
        self.stable_since: float | None = None
        # Entry/leave hysteresis timers.
        self.pending: str | None = None
        self.pending_since: float | None = None
        self.leave_since: float | None = None
        # The remembered physical source plus the tent re-entry grace.  Manual
        # mode selections are deliberately not part of this state: prompts and
        # direct entry follow the posture recognised by the sensor.
        self._source = _SourceState()

    # -- public state --------------------------------------------------------

    @property
    def posture_pending(self) -> bool:
        """A healthy hardware exit is waiting for a settled posture."""
        return (
            self.hardware is False and self.fresh
            and self.mode == MODE_DISABLED
            and self._source.hardware_release_pending
        )

    @property
    def pending_notification(self) -> int | None:
        """Epoch of the tent or stand confirmation prompt, or ``None``.

        The controller further gates this by ``manual == auto`` and the
        matching prompt setting; the policy only knows the posture itself.
        """

        return self.epoch if self.mode in (MODE_TENT_PROMPT, MODE_STAND_PROMPT) else None

    @property
    def pending_prompt(self) -> str | None:
        """Kind of confirmation prompt currently pending, if any."""

        if self.mode == MODE_TENT_PROMPT:
            return "tent"
        if self.mode == MODE_STAND_PROMPT:
            return "stand"
        return None

    @property
    def confirmation_ready(self) -> bool:
        """Whether the current prompt would be accepted right now.

        A prompt remains pending through the wider hold bands so small hinge
        movements do not restart the whole decision.  Notifications and UI
        controls must only advertise it while ``confirm``/``confirm_stand``
        would actually succeed; otherwise clicking can never change the mode
        and the same action is retried indefinitely.
        """

        if self.mode == MODE_TENT_PROMPT:
            return self._tent_confirmation_ready()
        if self.mode == MODE_STAND_PROMPT:
            return self._stand_confirmation_ready()
        return False

    # -- public actions ------------------------------------------------------

    def confirm(self, epoch: int) -> bool:
        """Turn a pending tent prompt into tent mode.

        Succeeds only when the prompt is still current (mode/epoch match), the
        hardware switch is known to be OFF, the sensor run is stable and the
        *current* geometry is still inside the documented tent zone.  This
        fixes review P2-6: the wider hold band must not confirm 45/150 deg.
        """

        if epoch != self.epoch or not self._tent_confirmation_ready():
            return False
        self._clear_timers()
        self._set_mode(MODE_TENT)
        return True

    def confirm_stand(self, epoch: int) -> bool:
        """Turn a pending stand prompt into stand mode.

        Succeeds only when the prompt is still current, the hardware switch is
        known to be OFF, the sensor run is stable and the *current* geometry is
        still inside the documented stand zone.
        """

        if epoch != self.epoch or not self._stand_confirmation_ready():
            return False
        self._clear_timers()
        self._set_mode(MODE_STAND)
        return True

    def _tent_confirmation_ready(self) -> bool:
        if (
            self.mode != MODE_TENT_PROMPT
            or self.hardware is not False
            or not self.stable
            or self.last_geometry is None
        ):
            return False
        _, _, pitch, roll, _ = self.last_geometry
        if not in_tent_zone(pitch, roll):
            return False
        # The prompt is entered from the smoothed orientation.  Require that
        # classification to agree as well so the action never outlives what the
        # policy currently recognises as a confirmable tent posture.
        pitch, roll, _ = self._classify()
        return in_tent_zone(pitch, roll)

    def _stand_confirmation_ready(self) -> bool:
        if (
            self.mode != MODE_STAND_PROMPT
            or self.hardware is not False
            or not self.stable
            or self.last_geometry is None
        ):
            return False
        _, _, _, _, flat = self.last_geometry
        if not in_stand_zone(flat):
            return False
        _, _, flat = self._classify()
        return in_stand_zone(flat)

    def reset_after_suspend(self) -> None:
        """Forget everything because the machine was suspended.

        ``CLOCK_BOOTTIME`` keeps running across suspend while the device pose
        may have changed; rebuilding from the pre-sleep anchors would recreate
        a stale mode/prompt on wakeup (review P2-7).  The epoch is bumped so
        any notification/prompt shown before suspend is rejected afterwards.
        """

        self.epoch += 1
        self.mode = MODE_DISABLED
        self.hardware = None
        # A wake-up is not a cold service start.  Do not arm the startup
        # baseline again; without fresh transition evidence the laptop origin
        # keeps the normal confirmation prompt.
        self._source = _SourceState(origin=_Origin.LAPTOP, startup_pending=False)
        self._reset_all()

    def update(
        self,
        now: float,
        reading: SensorReading,
        hardware: bool | None,
        *,
        tent_enabled: bool = True,
        tent_prompt: bool = True,
        stand_prompt: bool = False,
    ) -> None:
        """Ingest one sample and advance the state machine.

        ``hardware`` is the (already debounced) physical tablet switch state:
        ``True`` (folded), ``False`` (not folded) or ``None`` (unknown).
        ``tent_enabled`` permits automatic tent recognition at all; when false,
        an established tent mode is left immediately and the tent pose is
        treated as laptop posture.  ``tent_prompt``/``stand_prompt`` ask for an
        explicit confirmation
        before entering the corresponding mode from laptop posture.  When a
        prompt is disabled, a stable candidate is accepted automatically and a
        pending prompt for that mode resolves on the next stable sample.

        A freshly constructed policy adopts its first stable posture as the
        startup baseline without prompting.  This covers a service restart
        while the device is already folded into tent or stand posture.
        """

        # Invariant (P1-3): every return path prunes the smoothing history.
        self._prune(now)

        previous_hardware = self.hardware
        self.hardware = hardware
        if previous_hardware is True and hardware is not True:
            # Leaving hardware tablet mode: everything sampled before the fold
            # is meaningless, require a fresh stable run.  The switch may
            # release before the hinge reaches the tent zone, so remember the
            # physical tablet source until a settled laptop pose proves
            # otherwise.
            self._clear_run()
            self._source.origin = _Origin.NON_LAPTOP
            self._source.startup_pending = False
            self._source.hardware_release_pending = True
            self._set_mode(MODE_DISABLED)
        elif previous_hardware is not True and hardware is True:
            # Entering hardware tablet mode: the switch takes over.
            self._clear_run()

        self._ingest(reading)

        weather = (
            self.last_stamp is not None
            and 0.0 <= now - self.last_stamp <= self.config.stale_seconds
        )
        self.fresh = weather
        self.stable = bool(
            weather
            and self.stable_since is not None
            and now - self.stable_since >= self.config.stable_seconds
        )

        if hardware is True:
            # Hardware switch wins.  The sample was ingested above so the
            # freshness anchors stay current, but no smoothing history is
            # needed while folded and it must not grow (P1-3).
            self._clear_run()
            self._source.origin = _Origin.NON_LAPTOP
            self._source.startup_pending = False
            self._source.hardware_release_pending = False
            self._source.tent_exit_at = None
            self._set_mode(MODE_HARDWARE)
            return

        if hardware is None or not weather or self.last_geometry is None:
            # No trustworthy decision.  A single invalid sample cannot reach
            # this branch: freshness is measured from the last *valid* stamp,
            # so this fires only after stale_seconds without valid data (P2-5).
            if not weather or self.last_geometry is None:
                self._reset_all()
            else:
                self._clear_run()
            self._source.tent_exit_at = None
            self._set_mode(MODE_DISABLED)
            return

        if (
            self.mode in (MODE_HARDWARE, MODE_DISABLED)
            and not self._source.hardware_release_pending
        ):
            self._set_mode(MODE_LAPTOP)

        pitch, roll, flat = self._classify()
        if self.stable and flat is not None and flat < LAPTOP_MAX_FLAT:
            self._source.origin = _Origin.LAPTOP
            self._source.tent_exit_at = None
        if not tent_enabled and self.mode in (MODE_TENT, MODE_TENT_PROMPT):
            # The preference is an explicit off switch, not another debounce:
            # leave the software takeover on this update even while the sensor
            # still reports the tent pose.
            self._set_mode(MODE_LAPTOP, now)
            self._clear_timers()
            self._source.tent_exit_at = None
        tent = tent_enabled and in_tent_zone(pitch, roll)
        tent_hold = (
            tent_enabled
            and pitch is not None
            and TENT_HOLD_MIN_PITCH <= pitch <= TENT_HOLD_MAX_PITCH
            and abs(roll) <= TENT_HOLD_MAX_ROLL
        )
        stand = flat is not None and flat >= STAND_MIN_FLAT
        stand_hold = flat is not None and flat >= STAND_HOLD_MIN_FLAT

        if self._source.hardware_release_pending:
            # The released switch says nothing about the new posture.  Keep
            # the decision unconfirmed until fresh samples settle, then adopt
            # that pose directly instead of applying a temporary laptop target
            # and starting a second entry debounce.
            if not self.stable or flat is None:
                return
            self._source.hardware_release_pending = False
            if stand:
                self._set_mode(MODE_STAND)
            elif tent:
                self._set_mode(MODE_TENT)
            else:
                self._set_mode(MODE_LAPTOP)
            self._clear_timers()
            return

        if self.stable and self._source.startup_pending:
            # The service has no prior decision to preserve.  Adopt the first
            # stable recognised posture as the baseline so a restart while the
            # device is already in tent or stand does not ask for confirmation.
            self._clear_timers()
            if flat is not None and flat < LAPTOP_MAX_FLAT:
                self._source.origin = _Origin.LAPTOP
                self._set_mode(MODE_LAPTOP)
            else:
                self._source.origin = _Origin.NON_LAPTOP
                if tent:
                    self._set_mode(MODE_TENT)
                elif stand:
                    self._set_mode(MODE_STAND)
            self._source.startup_pending = False
            return

        if self.mode == MODE_TENT_PROMPT and not tent_hold:
            # The prompt is only valid while the device still looks like a
            # tent; leaving the wide hold band invalidates it.
            self._set_mode(MODE_LAPTOP)
        if self.mode == MODE_STAND_PROMPT and not stand_hold:
            # Like the tent prompt, a stand prompt is only valid while the
            # device is still in the wider stand hold band.
            self._set_mode(MODE_LAPTOP)
        if self.mode == MODE_STAND_PROMPT and not stand_prompt:
            # Disabling the preference while a prompt is pending should apply
            # the normal automatic decision immediately, without requiring the
            # user to leave and re-enter stand posture.
            if self.stable and stand:
                self._set_mode(MODE_STAND, now)
                self._clear_timers()
        if self.mode == MODE_TENT_PROMPT and not tent_prompt:
            # Mirror the stand-prompt behavior: disabling the prompt means the
            # stable tent posture no longer needs confirmation.
            if self.stable and tent:
                self._set_mode(MODE_TENT, now)
                self._clear_timers()

        desired: str | None = None
        if stand:
            if self.mode == MODE_STAND_PROMPT:
                desired = MODE_STAND_PROMPT
            elif (
                stand_prompt
                and self.mode == MODE_LAPTOP
                and self._source.origin is not _Origin.NON_LAPTOP
            ):
                # An explicit posture change from tent/stand already carries
                # intent; only the initial laptop-to-stand transition asks.
                desired = MODE_STAND_PROMPT
            else:
                desired = MODE_STAND
        elif tent and self.mode not in (MODE_TENT, MODE_TENT_PROMPT):
            # Leaving a non-laptop posture for a stable tent posture is an
            # explicit posture change, so it does not need the laptop-mode
            # confirmation prompt.  A recent tent exit may only skip the
            # prompt while no settled laptop pose has superseded that source.
            if (
                not tent_prompt
                or self._source.origin is _Origin.NON_LAPTOP
                or self.mode == MODE_STAND_PROMPT
                or self._recent_tent_exit(now)
            ):
                desired = MODE_TENT
            else:
                desired = MODE_TENT_PROMPT

        if self.stable and desired is not None and desired != self.mode:
            if self.pending != desired:
                self.pending = desired
                self.pending_since = now
            elif (
                self.pending_since is not None
                and now - self.pending_since >= self.config.enter_seconds
            ):
                self._set_mode(desired, now)
                self._clear_timers()
        else:
            self.pending = None
            self.pending_since = None

        hold = True
        if self.mode == MODE_TENT:
            hold = tent_hold
        elif self.mode == MODE_STAND:
            hold = stand_hold
        if not hold:
            if self.leave_since is None:
                self.leave_since = now
            leave_seconds = (
                self.config.stand_leave_seconds
                if self.mode == MODE_STAND
                else self.config.leave_seconds
            )
            # A departure only counts after the sensor has produced a stable
            # run in the new posture.  Without this gate, several seconds of
            # noisy samples below the hold edge could release stand mode and
            # immediately re-enter it when the signal settles.
            # A stable alternative software posture has its own entry timer.
            # Let that timer finish without releasing input via a temporary L
            # target. Motion, an abandoned candidate and real laptop poses
            # still follow the existing exit rules; stale data and hardware
            # changes have already revoked this path above.
            transferring = self.pending in (MODE_TENT, MODE_STAND)
            if self.stable and not transferring and now - self.leave_since >= leave_seconds:
                self._set_mode(MODE_LAPTOP, now)
                self._clear_timers()
        else:
            self.leave_since = None

    # -- internals -----------------------------------------------------------

    def _prune(self, now: float) -> None:
        window = self.config.history_seconds
        while self.history and now - self.history[0][0] > window:
            self.history.popleft()
        limit = self.config.history_max_samples
        while len(self.history) > limit:
            self.history.popleft()

    def _set_mode(self, mode: str, now: float | None = None) -> None:
        if self.mode == MODE_TENT and mode in (MODE_LAPTOP, MODE_STAND):
            # A settled laptop origin must not reopen the grace that its
            # sample just cleared.  Other posture exits keep the window;
            # hardware/D/reset transitions clear it instead.
            self._source.tent_exit_at = (
                now if self._source.origin is not _Origin.LAPTOP else None
            )
        elif mode == MODE_TENT:
            self._source.tent_exit_at = None
        if mode in (MODE_TENT, MODE_STAND):
            self._source.origin = _Origin.NON_LAPTOP
            self._source.startup_pending = False
        if mode != self.mode:
            self.epoch += 1
            self.mode = mode

    def _recent_tent_exit(self, now: float) -> bool:
        exit_at = self._source.tent_exit_at
        return exit_at is not None and (
            0.0 <= now - exit_at <= self.config.tent_reentry_seconds
        )

    def _clear_timers(self) -> None:
        self.pending = None
        self.pending_since = None
        self.leave_since = None

    def _clear_run(self) -> None:
        """Drop the stable-run evidence but keep the last valid geometry."""

        self.history.clear()
        self.last_sample = None
        self.stable_since = None
        self.stable = False
        self._clear_timers()

    def _reset_all(self) -> None:
        """Drop the stable-run evidence and the validity anchors as well."""

        self._clear_run()
        self.last_stamp = None
        self.last_geometry = None
        self.fresh = False

    def _ingest(self, reading: SensorReading) -> bool:
        """Record a valid sample; return ``False`` for invalid ones.

        Invalid samples must not update ``last_stamp``/``last_geometry``/
        ``last_sample`` and must not move the stability anchor (P2-5).
        """

        vector = reading.vector
        stamp = reading.stamp
        if vector is None or stamp is None or not math.isfinite(stamp):
            return False
        try:
            sample = geometry(vector)
        except ValueError:
            return False
        unit, magnitude, _, _, _ = sample
        if not (MIN_MAGNITUDE <= magnitude <= MAX_MAGNITUDE):
            return False
        if self.last_stamp is not None:
            if stamp <= self.last_stamp:
                # Duplicate or out-of-order report: not a new observation.
                return False
            if stamp - self.last_stamp > self.config.stale_seconds:
                # Valid data resumed after a long hole: old evidence is
                # obsolete, start a fresh run.
                self._clear_run()
        if self.last_sample is None:
            self.history.clear()
            self.stable_since = None
        else:
            dot = sum(
                a * b for a, b in zip(unit, self.last_sample[1], strict=True)
            )
            if dot < SETTLE_MAX_COS:
                # Orientation jump: rebuild the smoothing window from here.
                self.history.clear()
                self.stable_since = None
            elif self.stable_since is None:
                # The second sample of a consistent run anchors the run at the
                # previous sample, not at this one.
                self.stable_since = self.last_sample[0]
        self.history.append((stamp, unit))
        limit = self.config.history_max_samples
        while len(self.history) > limit:
            self.history.popleft()
        self.last_sample = (stamp, unit)
        self.last_stamp = stamp
        self.last_geometry = sample
        return True

    def _classify(self) -> tuple[float | None, float, float | None]:
        """Smooth the history and return ``(pitch, roll, flat)``."""

        if self.history:
            count = len(self.history)
            average = tuple(
                sum(sample[1][axis] for sample in self.history) / count
                for axis in range(3)
            )
        elif self.last_geometry is not None:
            average = self.last_geometry[0]
        else:
            # No retained evidence means there is nothing to classify.
            return None, 0.0, None
        try:
            _, _, pitch, roll, flat = geometry(average)
        except ValueError:
            # Rapid motion can cancel out to (almost) zero gravity; treat the
            # frame as unclassified instead of crashing the state machine.
            return None, 0.0, None
        return pitch, roll, flat
