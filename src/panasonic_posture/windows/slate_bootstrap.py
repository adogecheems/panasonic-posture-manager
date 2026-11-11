"""Read-only startup baseline for Windows convertible physical state."""
from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path

from ..model import SensorReading
from ..policy import STAND_HOLD_MIN_FLAT, geometry
from .convertible_slate import NativeSlateDevice, SlateDevice, SlateError
from .slate_journal import SlateJournal, SlateLease


def _users_root() -> Path:
    drive = os.environ.get('SystemDrive')
    if not drive:
        raise OSError('SystemDrive is unavailable')
    return Path(drive + '\\Users')


def pending_slate_leases(users_root: Path | None = None) -> tuple[SlateLease, ...]:
    """Load all readable per-user CSM recovery leases without guessing."""

    root = Path(users_root) if users_root is not None else _users_root()
    try:
        profiles = tuple(root.iterdir())
    except OSError as error:
        raise SlateError('cannot inspect Windows user profiles') from error
    leases: list[SlateLease] = []
    unreadable = False
    for profile in profiles:
        journal = (
            profile / 'AppData' / 'Local' / 'PanasonicPostureManager'
            / 'native-display-slate.json'
        )
        try:
            if journal.is_file():
                lease = SlateJournal(journal).load()
                if lease is None:
                    unreadable = True
                else:
                    leases.append(lease)
        except OSError as error:
            raise SlateError('cannot inspect the slate recovery journal') from error
    if unreadable:
        raise SlateError('an unreadable slate recovery journal requires an ETW event')
    return tuple(leases)


def read_bootstrap_slate_state(
    *,
    users_root: Path | None = None,
    device_factory: Callable[[], SlateDevice] = NativeSlateDevice,
) -> bool | None:
    """Read the raw indicator once when no application-owned CSM lease exists."""

    leases = pending_slate_leases(users_root)
    if len(leases) > 1:
        raise SlateError('multiple slate recovery journals are ambiguous')
    if leases:
        # The current CSM may still be software-owned. Require independent
        # laptop geometry before granting an initial physical state.
        return None
    with device_factory() as device:
        return device.read()


class StartupPhysicalBaseline:
    """Combine a one-time CSM read with stable non-tablet geometry when needed."""

    def __init__(
        self,
        *,
        slate_reader: Callable[[], bool | None],
        sensor,
        clock: Callable[[], float],
        settle: float = .5,
    ) -> None:
        self.slate_reader, self.sensor, self.clock = slate_reader, sensor, clock
        self.settle = settle
        self._slate_loaded = False
        self._slate: bool | None = None
        self._stable_since: float | None = None
        self._last_stamp: float | None = None

    def __call__(self) -> bool | None:
        if not self._slate_loaded:
            try:
                self._slate = self.slate_reader()
            except (OSError, ValueError):
                # A missing/ambiguous CSM reading still permits the safe
                # laptop-geometry fallback below.
                self._slate = None
            self._slate_loaded = True
        if self._slate is False:
            return False
        if self._slate is not None and self._slate is not True:
            raise ValueError('startup slate state is not boolean')
        reading = self.sensor.reading()
        now = self.clock()
        if not (
            isinstance(reading, SensorReading)
            and reading.valid
            and reading.stamp is not None
            and reading.vector is not None
            and 0 <= now - reading.stamp <= 1.
        ):
            self._stable_since = self._last_stamp = None
            return None
        try:
            flat = geometry(reading.vector)[4]
        except ValueError:
            flat = None
        if flat is None or flat >= STAND_HOLD_MIN_FLAT:
            self._stable_since = None
            self._last_stamp = reading.stamp
            return None
        if self._last_stamp is None or reading.stamp > self._last_stamp:
            self._last_stamp = reading.stamp
            if self._stable_since is None:
                self._stable_since = reading.stamp
            elif reading.stamp - self._stable_since >= self.settle:
                return False
        return None


__all__ = [
    'StartupPhysicalBaseline', 'pending_slate_leases',
    'read_bootstrap_slate_state',
]
