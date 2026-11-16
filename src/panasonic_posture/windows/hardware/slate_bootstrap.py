"""Read-only startup and fresh geometry baselines for convertible state."""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
import os
from pathlib import Path
import threading

from panasonic_posture.model import SensorReading
from panasonic_posture.policy import STAND_HOLD_MIN_FLAT, geometry
from panasonic_posture.windows.hardware.convertible_slate import (
    NativeSlateDevice,
    SlateDevice,
    SlateError,
)
from panasonic_posture.windows.hardware.slate_journal import SlateJournal, SlateLease


def _users_root() -> Path:
    drive = os.environ.get('SystemDrive')
    if not drive:
        raise OSError('SystemDrive is unavailable')
    return Path(drive + '\\Users')


def _legacy_journals(users_root: Path | None):
    root = Path(users_root) if users_root is not None else _users_root()
    for profile in root.iterdir():
        yield (profile / 'AppData' / 'Local' / 'PanasonicPostureManager'
               / 'native-display-slate.json')


def has_legacy_slate_journal(users_root: Path | None = None) -> bool:
    # A user's file can only narrow admission. Its contents are unnecessary.
    for journal in _legacy_journals(users_root):
        try:
            journal.lstat()
        except FileNotFoundError:
            continue
        return True
    return False


def pending_slate_leases(users_root: Path | None = None) -> tuple[SlateLease, ...]:
    """Load all readable per-user CSM recovery leases without guessing."""

    try:
        journals = tuple(_legacy_journals(users_root))
    except OSError as error:
        raise SlateError('cannot inspect Windows user profiles') from error
    leases: list[SlateLease] = []
    unreadable = False
    for journal in journals:
        try:
            if journal.is_file():
                # Bootstrap is read-only even for malformed user records;
                # isolation belongs to the qualified session's recovery path.
                lease = SlateJournal(journal).load(quarantine=False)
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


class AsyncSlateReader:
    """One read-only startup query, independent of the service owner loop.

    User paths can stall even a bounded read. Keep a single daemon worker
    alive until that read finishes; a late result never writes hardware.
    """

    def __init__(self, reader: Callable[[], bool | None]) -> None:
        self.reader = reader
        self._task: Future | None = None

    @property
    def pending(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self._task is None:
            task = self._task = Future()
            def read() -> None:
                try:
                    task.set_result(self.reader())
                except BaseException as error:
                    task.set_exception(error)
            threading.Thread(target=read, name='windows-slate-bootstrap', daemon=True).start()

    def __call__(self) -> bool | None:
        self.start()
        if not self._task.done():
            return None
        return self._task.result()


class StartupPhysicalBaseline:
    """Accept a clean startup indicator, checking conflicting geometry."""

    def __init__(
        self,
        *,
        slate_reader: Callable[[], bool | None],
        sensor,
        clock: Callable[[], float],
        settle: float = .5,
        minimum_stamp: float | None = None,
    ) -> None:
        self.slate_reader, self.sensor, self.clock = slate_reader, sensor, clock
        self.settle = settle
        self.minimum_stamp = minimum_stamp
        self._slate_loaded = False
        self._slate: bool | None = None
        self._geometry_conflict = False
        self._stable_since: float | None = None
        self._last_stamp: float | None = None

    def __call__(self) -> bool | None:
        if not self._slate_loaded:
            if isinstance(self.slate_reader, AsyncSlateReader):
                self.slate_reader.start()
                if self.slate_reader.pending:
                    return None
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
            and (self.minimum_stamp is None or reading.stamp > self.minimum_stamp)
        ):
            self._stable_since = self._last_stamp = None
            # A missing sample cannot clear a conflict already observed.
            return None if self._geometry_conflict else self._slate
        try:
            flat = geometry(reading.vector)[4]
        except ValueError:
            flat = None
        if flat is None or flat >= STAND_HOLD_MIN_FLAT:
            self._stable_since = None
            self._last_stamp = reading.stamp
            # Only the clean read-only startup query can supply True. A
            # pending recovery lease supplies None, so geometry alone never
            # asserts tablet state. Without a conflict, preserve the queried
            # indicator instead of falling through to the timeout's False.
            return None if flat is None and self._geometry_conflict else self._slate
        self._geometry_conflict = True
        if self._last_stamp is None or reading.stamp > self._last_stamp:
            self._last_stamp = reading.stamp
            if self._stable_since is None:
                self._stable_since = reading.stamp
            elif reading.stamp - self._stable_since >= self.settle:
                return False
        return None


__all__ = [
    'AsyncSlateReader', 'StartupPhysicalBaseline', 'pending_slate_leases',
    'read_bootstrap_slate_state',
]
