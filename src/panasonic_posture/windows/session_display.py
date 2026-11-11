"""Nonprivileged display qualification shared by both Windows compositions."""
from collections.abc import Callable
import logging
from typing import Protocol

from ..contracts import ErrorCode
from ..model import HardwareStatus, StateSnapshot, Target
from ..session.failures import DisplayError
from ..session.ports import DisplayBackend, DisplayRefresh


log = logging.getLogger(__name__)


class SessionContext(Protocol):
    def control_available(self) -> bool: ...
    def poll(self) -> None: ...
    def close(self) -> None: ...


class SlatePosture(Protocol):
    """Bounded notification of a convertible posture; the OS owns response."""

    def sync_mode(self, mode: str) -> bool: ...
    def wait_for_display_handoff(self) -> None: ...
    def release(self) -> None: ...
    def close(self) -> None: ...


def slate_qualified_hardware(query: Callable[[], tuple[bool | None, str]],
                             owned: Callable[[], bool | None],
                             settling: Callable[[], bool] = lambda: False):
    """Separate our slate notification from an independent physical change."""
    def qualified():
        hardware, status = query()
        if settling():
            return False, HardwareStatus.OK
        target = owned()
        if target is None or status != HardwareStatus.OK or type(hardware) is not bool:
            return hardware, status
        if hardware != target:
            return None, HardwareStatus.AMBIGUOUS
        # The raw CSM currently reflects our own notification, not a physical
        # takeover. An external change to the opposite state fails above.
        return False, HardwareStatus.OK
    return qualified


class QualifiedDisplay:
    def __init__(
        self,
        display: DisplayBackend,
        context: SessionContext,
        slate: SlatePosture | None = None,
    ) -> None:
        self.display, self.context, self.slate = display, context, slate

    def _require_available(self) -> None:
        if not self.context.control_available():
            raise DisplayError(
                ErrorCode.DISPLAY_UNAVAILABLE,
                'native session control is unavailable',
            )

    def _release_slate(self) -> None:
        if self.slate is None:
            return
        try:
            self.slate.release()
        except (OSError, ValueError):
            # A display rollback remains the authoritative failure. Session
            # shutdown will make another bounded release attempt.
            pass

    def _sync_slate(self, target: Target) -> None:
        if self.slate is None or target.mode == 'unavailable':
            return
        try:
            self.slate.sync_mode(target.mode)
        except (OSError, ValueError) as error:
            raise DisplayError(
                ErrorCode.DISPLAY_UNAVAILABLE,
                'convertible-slate-notification-failed',
            ) from error

    def _restore_after_failed_transaction(self) -> None:
        try:
            self.display.restore()
        except (OSError, RuntimeError, ValueError) as error:
            # Keep the original transaction failure visible. The durable
            # display journal is retained whenever this cleanup cannot finish.
            log.warning('Windows display rollback after a failed apply: %s', error)

    def apply(self, target: Target) -> None:
        if target.mode == 'unavailable':
            self.display.apply(target)  # Existing ownership may still restore.
            self._release_slate()
            return
        self._require_available()
        try:
            # Send the platform posture first and let Windows begin its native
            # shell/orientation response without a competing rotation lock.
            # The short handoff wait lets that response settle before the
            # fixed target verifies or corrects the direction.
            if self.slate is not None:
                self._sync_slate(target)
                self._require_available()
                wait = getattr(self.slate, 'wait_for_display_handoff', None)
                if wait is not None:
                    wait()
                self._require_available()
            self.display.apply(target)
            if not self.context.control_available():
                raise DisplayError(
                    ErrorCode.DISPLAY_UNAVAILABLE,
                    'session changed during display apply',
                )
        except BaseException:
            self._restore_after_failed_transaction()
            self._release_slate()
            raise

    def hold_orientation(self) -> None:
        self._require_available()
        try:
            self.display.hold_orientation()
            if not self.context.control_available():
                raise DisplayError(
                    ErrorCode.DISPLAY_UNAVAILABLE,
                    'session changed during display hold',
                )
        except BaseException:
            self._restore_after_failed_transaction()
            self._release_slate()
            raise

    def prepare_hardware_release(self) -> None:
        prepare = getattr(self.display, 'prepare_hardware_release', None)
        if prepare is None:
            return
        self._require_available()
        try:
            prepare()
        except BaseException:
            self._restore_after_failed_transaction()
            self._release_slate()
            raise

    def restore(self, normal: bool = False) -> None:
        try:
            self.display.restore(normal=normal)
        finally:
            self._release_slate()


class QualifiedRefresh:
    """Fence ongoing writes against current local qualification and target."""

    def __init__(self, display: DisplayRefresh, context: SessionContext,
                 current: Callable[[], StateSnapshot]) -> None:
        self.display, self.context, self.current = display, context, current

    def needs_refresh(self, target: Target) -> bool:
        return self.display.needs_refresh(target)

    def _require_current(self, expected: StateSnapshot) -> StateSnapshot:
        if not self.context.control_available():
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'native-session-control-unavailable')
        current = self.current()
        if (current.instance != expected.instance or current.generation != expected.generation or
                current.target != expected.target or not current.session_connected or
                not current.supported or current.sensor_error or current.error_code or
                current.hardware is not False or current.hardware_status != HardwareStatus.OK):
            raise DisplayError(ErrorCode.DISPLAY_UNAVAILABLE, 'ongoing-display-target-no-longer-current')
        return current

    def refresh(self, state: StateSnapshot) -> None:
        current = self._require_current(state)
        self.display.refresh(current)
        self._require_current(state)
