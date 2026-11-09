"""Journalled software-tablet rotation on the serialized display worker.

The composition must supply a verified mapping and live orientation reader.
This component never starts a polling thread or enables a frontend capability.
Native Windows rotation and the physical slate switch retain precedence.
"""
from collections.abc import Callable, Sequence
import time

from ..contracts import ErrorCode
from ..model import HardwareStatus, StateSnapshot, Target
from ..session.failures import DisplayError
from .display import WindowsDisplay
from .orientation import OrientationError, OrientationReading, QuadrantPolicy


class WindowsTabletDisplay(WindowsDisplay):
    def __init__(self, *, journal, orientation: Callable[[], OrientationReading | None],
                 mapping: Sequence[int], api=None, hardware_query=None,
                 clock: Callable[[], float] = time.monotonic):
        self.orientation, self.clock = orientation, clock
        self.policy = QuadrantPolicy(mapping=mapping)
        self._tablet = False
        super().__init__(journal=journal, api=api, hardware_query=hardware_query)

    @staticmethod
    def needs_refresh(target: Target) -> bool:
        return target.mode == 'tablet'

    def _direction(self) -> int | None:
        try:
            return self.policy.choose(self.orientation(), now=self.clock())
        except OrientationError as error:
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, str(error)) from error

    def _validate(self, state, mode, transform):
        # Software rotation replaces an unavailable OS rotator; it must never
        # compete with an active one or modify the system's rotation policy.
        super()._validate(state, 'stand' if mode == 'tablet' else mode, transform)

    def _releases_rotation_lock(self, mode):
        # Software tablet rotation still needs the system rotator held so two
        # orientation writers cannot race for the same panel.
        return False if mode == 'tablet' else super()._releases_rotation_lock(mode)

    def _verify_owned(self, transform: int | None = None):
        current = self._read_state()
        self._validate(current, 'tablet', current.transform)
        if not self.owned or current.identity != tuple(self.owned['spec']):
            raise DisplayError(ErrorCode.DISPLAY_IDENTITY, 'tablet-display-ownership-changed')
        if current.lock_supported and not current.lock:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, 'native-auto-rotation-took-over')
        if transform is not None and current.transform != transform:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, 'tablet-rotation-changed-before-receipt')
        return current

    def apply(self, target: Target | dict) -> None:
        self._tablet = False
        self.policy.reset()
        mode = target.get('mode') if isinstance(target, dict) else target.mode
        try:
            if mode == 'tablet':
                self._direction()  # Missing or stale data cannot begin ownership.
            super().apply(target)
            if mode == 'tablet':
                self._verify_owned(0)
                self._tablet = True
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def refresh(self, state: StateSnapshot) -> None:
        """Refresh one qualified generation; the runtime verifies it afterward."""
        try:
            if (not self._tablet or not isinstance(state, StateSnapshot) or
                    state.target.mode != 'tablet' or not state.supported or
                    state.hardware is not False or state.hardware_status != HardwareStatus.OK or
                    not state.session_connected or state.sensor_error):
                raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'tablet-session-no-longer-ready')
            transform = self._direction()
            current = self._verify_owned()
            if transform is None:
                return  # Flat/unstable readings retain the current direction.
            if current.transform not in {self.owned.get('written_transform'), self.owned.get('pending_transform')}:
                # A user/system edit becomes the next restoration baseline.
                # Persist it before taking fresh ownership of a new direction.
                document = self.owned.copy()
                document['original_transform'] = current.transform
                document['written_transform'] = None
                document.pop('pending_transform', None)
                self.journal.save(document)
                self.owned = document
            self._write_owned_rotation(current, transform)
            self._verify_owned(transform)
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def hold_orientation(self):
        self._tablet = False
        self.policy.reset()
        super().hold_orientation()

    def restore(self, normal: bool = False, *, release_lock: bool = False, keep_journal: bool = False) -> None:
        # Once dynamic tablet rotation is active, an OS rotator takeover is a
        # fresh platform-owned direction.  Resolve that handoff before ending
        # the dynamic ownership state used by ``refresh``.  Fixed postures and
        # failed tablet startup retain the normal rollback contract.
        if self._tablet and self.owned and not self._hardware():
            self._relinquish_native_rotation()
        self._tablet = False
        self.policy.reset()
        super().restore(normal, release_lock=release_lock, keep_journal=keep_journal)
