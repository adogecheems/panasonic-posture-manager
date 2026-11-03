"""Stable posture drawing for the user interface.

Presentation never gates hardware decisions; it only decides which
illustration to show.  The UI has no separate "waiting for posture" state:
The initial drawing is the laptop pose, and the last known pose stays visible
while the device moves or the sensor is temporarily unavailable.  Fresh,
non-unknown candidates still need ``settle_seconds`` before replacing it.
"""

from __future__ import annotations

from .model import Pose
from .policy import PolicyConfig

# How long a new recognised pose must persist before it replaces the drawing.
SETTLE_SECONDS = 0.3


class PosePresentation:
    """Debounced pose value for the UI illustration."""

    def __init__(
        self,
        config: PolicyConfig | None = None,
        settle_seconds: float = SETTLE_SECONDS,
        stale_seconds: float | None = None,
    ) -> None:
        self.config = config if config is not None else PolicyConfig()
        self.settle_seconds = settle_seconds
        self.stale_seconds = (
            self.config.stale_seconds if stale_seconds is None else stale_seconds
        )
        self.value = Pose.LAPTOP
        self.pending: Pose | None = None
        self.pending_since: float | None = None

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
            # Keep the last known drawing.  The internal policy still releases
            # the software takeover on stale data; the UI simply has no
            # separate "waiting for posture" state.
            self._reset()
            return self.value

        if pose == Pose.UNKNOWN:
            # Do not let an unclassified frame discard an in-flight candidate.
            return self.value

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

    def _reset(self) -> None:
        self.pending = None
        self.pending_since = None
