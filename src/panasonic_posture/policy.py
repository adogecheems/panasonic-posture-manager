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

from .model import SensorReading

# --- Internal modes ---------------------------------------------------------

MODE_LAPTOP = "L"
MODE_TENT_PROMPT = "P"
MODE_TENT = "T"
MODE_STAND = "S"
MODE_HARDWARE = "H"
MODE_DISABLED = "D"

POLICY_MODES = (
    MODE_LAPTOP,
    MODE_TENT_PROMPT,
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

# Hysteresis bands: once in P/T/S the posture is held until the device leaves
# a wider band for ``leave_seconds``.
TENT_HOLD_MIN_PITCH = 45.0
TENT_HOLD_MAX_PITCH = 155.0
TENT_HOLD_MAX_ROLL = 40.0
STAND_MIN_FLAT = 155.0
STAND_HOLD_MIN_FLAT = 140.0

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
    # A confirmed tent mode may be restored without another prompt when the
    # device briefly leaves and re-enters the tent zone (for example while the
    # user adjusts the hinge).  A stale exit never skips confirmation.
    tent_reentry_seconds: float = 5.0
    stale_seconds: float = 2.0
    history_seconds: float = 0.75
    stable_seconds: float = 0.45
    history_max_samples: int = 128

    def __post_init__(self) -> None:
        for name in (
            "enter_seconds",
            "leave_seconds",
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


class Policy:
    """Posture state machine fed by sensor samples and the hardware switch.

    The public contract is small: call :meth:`update` for every sample, read
    :attr:`mode`/``stable``, call :meth:`confirm` for the tent prompt and
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
        # Timestamp of the last automatic exit from a *confirmed* tent mode.
        # Set only for L/S transitions so hardware takeover, suspend and lost
        # sensor data can never turn into an implicit confirmation.
        self._tent_exit_at: float | None = None

    # -- public state --------------------------------------------------------

    @property
    def pending_notification(self) -> int | None:
        """Epoch of the tent confirmation prompt, or ``None``.

        The controller further gates this by ``manual == auto`` and the
        ``tent_prompt`` setting; the policy only knows the posture itself.
        """

        return self.epoch if self.mode == MODE_TENT_PROMPT else None

    # -- public actions ------------------------------------------------------

    def confirm(self, epoch: int) -> bool:
        """Turn a pending tent prompt into tent mode.

        Succeeds only when the prompt is still current (mode/epoch match), the
        hardware switch is known to be OFF, the sensor run is stable and the
        *current* geometry is still inside the documented tent zone.  This
        fixes review P2-6: the wider hold band must not confirm 45/150 deg.
        """

        if (
            self.mode != MODE_TENT_PROMPT
            or epoch != self.epoch
            or self.hardware is not False
            or not self.stable
            or self.last_geometry is None
        ):
            return False
        _, _, pitch, roll, _ = self.last_geometry
        if not in_tent_zone(pitch, roll):
            return False
        self._clear_timers()
        self._set_mode(MODE_TENT)
        return True

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
        self._tent_exit_at = None
        self._reset_all()

    def update(
        self,
        now: float,
        reading: SensorReading,
        hardware: bool | None,
    ) -> None:
        """Ingest one sample and advance the state machine.

        ``hardware`` is the (already debounced) physical tablet switch state:
        ``True`` (folded), ``False`` (not folded) or ``None`` (unknown).
        """

        # Invariant (P1-3): every return path prunes the smoothing history.
        self._prune(now)

        previous_hardware = self.hardware
        self.hardware = hardware
        if previous_hardware is True and hardware is not True:
            # Leaving hardware tablet mode: everything sampled before the fold
            # is meaningless, require a fresh stable run.
            self._clear_run()
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
            self._tent_exit_at = None
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
            self._tent_exit_at = None
            self._set_mode(MODE_DISABLED)
            return

        if self.mode in (MODE_HARDWARE, MODE_DISABLED):
            self._set_mode(MODE_LAPTOP)

        pitch, roll, flat = self._classify()
        tent = in_tent_zone(pitch, roll)
        tent_hold = (
            pitch is not None
            and TENT_HOLD_MIN_PITCH <= pitch <= TENT_HOLD_MAX_PITCH
            and abs(roll) <= TENT_HOLD_MAX_ROLL
        )
        stand = flat is not None and flat >= STAND_MIN_FLAT
        stand_hold = flat is not None and flat >= STAND_HOLD_MIN_FLAT

        if self.mode == MODE_TENT_PROMPT and not tent_hold:
            # The prompt is only valid while the device still looks like a
            # tent; leaving the wide hold band invalidates it.
            self._set_mode(MODE_LAPTOP)

        desired: str | None = None
        if stand:
            desired = MODE_STAND
        elif tent and self.mode not in (MODE_TENT, MODE_TENT_PROMPT):
            # Re-entering shortly after a confirmed tent mode is a debounce,
            # not a new decision: skip the confirmation prompt and restore T.
            if (
                self._tent_exit_at is not None
                and 0.0 <= now - self._tent_exit_at
                <= self.config.tent_reentry_seconds
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
            elif now - self.leave_since >= self.config.leave_seconds:
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
            # Only a posture-driven exit from confirmed tent mode opens the
            # re-entry window.  Hardware/D/reset transitions clear it instead.
            self._tent_exit_at = now
        elif mode == MODE_TENT:
            self._tent_exit_at = None
        if mode != self.mode:
            self.epoch += 1
            self.mode = mode

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
