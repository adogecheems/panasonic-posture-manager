"""Stable posture drawing for the user interface.

Presentation never gates hardware decisions; it only decides which
illustration to show.  The last confirmed pose stays visible while the device
is moving or while the posture classifier briefly returns ``unknown``, but for
at most ``unknown_sticky_seconds`` (1 s) so a dead or silent sensor cannot pin
a wrong drawing forever (review P3: presentation must not stay frozen).
"""

from __future__ import annotations

from .model import Pose
from .policy import PolicyConfig

# How long a new recognised pose must persist before it replaces the drawing.
SETTLE_SECONDS = 0.3
# Hard cap on keeping the last drawing while the candidate is unknown.
UNKNOWN_STICKY_SECONDS = 1.0


class PosePresentation:
    """Debounced pose value for the UI illustration."""

    def __init__(
        self,
        config: PolicyConfig | None = None,
        settle_seconds: float = SETTLE_SECONDS,
        stale_seconds: float | None = None,
        unknown_sticky_seconds: float = UNKNOWN_STICKY_SECONDS,
    ) -> None:
        self.config = config if config is not None else PolicyConfig()
        self.settle_seconds = settle_seconds
        self.stale_seconds = (
            self.config.stale_seconds if stale_seconds is None else stale_seconds
        )
        self.unknown_sticky_seconds = unknown_sticky_seconds
        self.value = Pose.UNKNOWN
        self.pending: Pose | None = None
        self.pending_since: float | None = None
        self.unknown_since: float | None = None

    # -- public API ----------------------------------------------------------

    def update(
        self,
        now: float,
        candidate: Pose | str,
        hardware: bool | None,
        stamp: float | None,
    ) -> Pose:
        """Return the pose to draw for this tick."""

        pose = candidate if isinstance(candidate, Pose) else Pose(candidate)

        if hardware is True:
            # The physical switch confirms tablet mode without any delay.
            self.value = Pose.TABLET
            self._reset()
            return self.value

        if stamp is None or not 0.0 <= now - stamp <= self.stale_seconds:
            # True staleness is never hidden: the drawing must not claim a
            # posture the sensor can no longer support.
            self.value = Pose.UNKNOWN
            self._reset()
            return self.value

        if pose == Pose.UNKNOWN:
            return self._sticky_unknown(now)

        self.unknown_since = None
        if pose == self.value:
            self.pending = None
            self.pending_since = None
            return self.value
        if pose != self.pending:
            # New candidate: it has to persist for settle_seconds.
            self.pending = pose
            self.pending_since = now
            return self.value
        if (
            self.pending_since is not None
            and now - self.pending_since >= self.settle_seconds
        ):
            self.value = pose
            self._reset()
        return self.value

    # -- internals -----------------------------------------------------------

    def _sticky_unknown(self, now: float) -> Pose:
        if self.value == Pose.UNKNOWN:
            # Nothing confirmed yet; keep an in-flight candidate alive so a
            # single missing sample does not restart its dwell timer.
            return self.value
        if self.unknown_since is None:
            self.unknown_since = now
        if now - self.unknown_since >= self.unknown_sticky_seconds:
            self.value = Pose.UNKNOWN
            self._reset()
        return self.value

    def _reset(self) -> None:
        self.pending = None
        self.pending_since = None
        self.unknown_since = None
