"""Nonprivileged display qualification shared by both Windows compositions."""
from collections.abc import Callable
import logging
from typing import Protocol

from ..contracts import ErrorCode
from ..model import HardwareStatus, StateSnapshot, Target
from ..session.failures import DisplayError, ServiceError
from ..session.ports import DisplayBackend, DisplayRefresh


log = logging.getLogger(__name__)


class SessionContext(Protocol):
    def control_available(self) -> bool: ...
    def poll(self) -> None: ...
    def close(self) -> None: ...


class SlatePosture(Protocol):
    """Bounded notification of a convertible posture; the OS owns response."""

    def sync_mode(self, mode: str) -> bool: ...
    def release(self) -> None: ...
    def close(self) -> None: ...


class SessionHardwareQuery:
    """Read the physical switch state from the authenticated session service.

    The hardware host derives this field only from ETW/GPIO observations.  A
    missing value, a stale snapshot or a failed RPC keeps CSM writes disabled
    instead of falling back to the indicator this process may have written.
    """

    def __init__(self, state_loader: Callable[[], StateSnapshot]) -> None:
        self.state_loader = state_loader

    def __call__(self) -> tuple[bool | None, HardwareStatus]:
        try:
            state = self.state_loader()
            hardware = getattr(state, 'hardware', None)
            status = getattr(state, 'hardware_status', None)
        except (OSError, RuntimeError, ServiceError, ValueError, TypeError):
            return None, HardwareStatus.READ_ERROR
        if hardware is None:
            return None, status if isinstance(status, HardwareStatus) else HardwareStatus.ABSENT
        if type(hardware) is not bool or status != HardwareStatus.OK:
            return None, HardwareStatus.READ_ERROR
        return hardware, HardwareStatus.OK


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
            if self.slate is not None and target.mode == 'hardware':
                # Physical tablet is the one case where Windows owns rotation.
                # Publish the platform posture first and let the native
                # handoff proceed before relinquishing the CCD lock.
                self._sync_slate(target)
                self._require_available()
                self.display.apply(target)
            else:
                # Software modes must own the CCD rotation lock before CSM can
                # make Windows start its own tablet-orientation response.
                # Notifying first creates two competing rotation sources and
                # visible multi-stage jumps.
                self.display.apply(target)
                if not self.context.control_available():
                    raise DisplayError(
                        ErrorCode.DISPLAY_UNAVAILABLE,
                        'session changed before slate notification',
                    )
                self._sync_slate(target)
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
