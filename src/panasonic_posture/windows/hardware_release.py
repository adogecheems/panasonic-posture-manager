"""Disabled release-preparation coordinator for the physical tablet switch.

The coordinator survives as the runtime's optional release fast-path hook,
but preparation is a documented no-op on this platform: see
:class:`HardwareReleaseCoordinator` for the on-device finding.
"""
from ..model import StateSnapshot


class HardwareReleaseCoordinator:
    """No-op release preparation after the on-device pre-lock finding.

    Field logs showed that Windows overrides the rotation lock while the
    convertible-slate metric is released. Pre-locking the pending direction
    before the switch opened therefore never survived the release: the panel
    still jumped to the native direction, and the pre-lock failure path added
    a rollback rotation of its own. The bounded read-only wait in
    ``SessionRuntime`` now owns the whole handoff, so preparation is disabled
    and the fast-path evidence is never produced.
    """

    def __init__(self, display: object) -> None:
        del display

    def target_for(self, instance: str) -> str | None:
        """Return ``None``: release preparation is disabled on this platform."""

        del instance
        return None

    def prepare(self, state: StateSnapshot, pose: str | None) -> None:
        """Never touch the display before the physical switch releases."""

        del state, pose
