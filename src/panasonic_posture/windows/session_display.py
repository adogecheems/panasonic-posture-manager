"""Nonprivileged display qualification shared by both Windows compositions."""
from collections.abc import Callable
import logging
import time
from typing import Protocol

from ..contracts import ErrorCode
from ..model import HardwareStatus, StateSnapshot, Target
from ..session.failures import DisplayError, ServiceError, TRANSIENT_CONTROL_DETAIL
from ..session.ports import DisplayBackend, DisplayRefresh


log = logging.getLogger(__name__)
HARDWARE_QUERY_RETRY_SECONDS = 0.1
HARDWARE_QUERY_ATTEMPTS = 6
SLATE_SETTLE_SECONDS = 0.5


class SessionContext(Protocol):
    def control_available(self) -> bool: ...
    def unattended(self) -> bool: ...
    def poll(self) -> None: ...
    def close(self) -> None: ...


def transient_context_blip(context: SessionContext) -> bool:
    """True when qualification looks lost only through a stale observation.

    The session is still the attended, unlocked console, so losing
    ``control_available`` is a transient timing artifact (for example the WTS
    presence query taking longer than its freshness window). Genuine loss
    (lock, suspend, console switch or a failed observer) is never a blip.
    """

    snapshot = getattr(context, "snapshot", None)
    if not callable(snapshot):
        return False
    try:
        state = snapshot()
    except Exception:  # noqa: BLE001 - snapshot failures stay conservative
        return False
    if not isinstance(state, dict) or state.get("suspended") is True:
        return False
    presence = state.get("presence")
    if not isinstance(presence, dict):
        return False
    return presence.get("active") is True and presence.get("locked") is False


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

    def __init__(
        self,
        state_loader: Callable[[], StateSnapshot],
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state_loader = state_loader
        self.sleep = sleep

    def __call__(self) -> tuple[bool | None, HardwareStatus]:
        for attempt in range(HARDWARE_QUERY_ATTEMPTS):
            try:
                state = self.state_loader()
                hardware = state.hardware
                status = state.hardware_status
                break
            except ServiceError:
                # The service returned an authoritative failure. Retrying a
                # rejected session or failed operation cannot renew ownership.
                return None, HardwareStatus.READ_ERROR
            except TimeoutError:
                # A full request timeout already consumed the display worker's
                # useful handoff budget; more retries would only amplify it.
                return None, HardwareStatus.READ_ERROR
            except (OSError, RuntimeError) as error:
                if attempt + 1 >= HARDWARE_QUERY_ATTEMPTS:
                    return None, HardwareStatus.READ_ERROR
                # The service resets disconnected pipe instances on its owner
                # loop. Give that loop time to make the instance connectable
                # again instead of treating a transport race as lost hardware.
                log.debug(
                    'retrying transient hardware-state read after %s: %s',
                    HARDWARE_QUERY_RETRY_SECONDS,
                    error,
                )
                self.sleep(HARDWARE_QUERY_RETRY_SECONDS)
            except (ValueError, TypeError):
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
        *,
        settle: Callable[[float], None] = time.sleep,
    ) -> None:
        self.display, self.context, self.slate = display, context, slate
        self.settle = settle
        self._last_unavailable_log = float('-inf')

    def _require_available(self) -> None:
        if not self.context.control_available():
            if time.monotonic() - self._last_unavailable_log >= 2.0:
                self._last_unavailable_log = time.monotonic()
                snapshot = getattr(self.context, 'snapshot', None)
                log.warning(
                    'native session control is unavailable for display work: %s',
                    snapshot() if callable(snapshot) else 'no context snapshot',
                )
            if transient_context_blip(self.context):
                # The session is still attended; the loss is a stale fast
                # observation. Fail this request without tearing anything
                # down so the next attempt succeeds with no visible change.
                raise DisplayError(
                    ErrorCode.DISPLAY_UNAVAILABLE,
                    TRANSIENT_CONTROL_DETAIL,
                )
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

    def _sync_slate_mode(self, mode: str) -> None:
        if self.slate is None or mode == 'unavailable':
            return
        try:
            self.slate.sync_mode(mode)
        except (OSError, ValueError) as error:
            raise DisplayError(
                ErrorCode.DISPLAY_UNAVAILABLE,
                'convertible-slate-notification-failed',
            ) from error

    def _sync_slate(self, target: Target) -> None:
        self._sync_slate_mode(target.mode)

    def _release_slate_after_failure(self, error: BaseException) -> None:
        if (
            isinstance(error, DisplayError)
            and error.detail == TRANSIENT_CONTROL_DETAIL
        ):
            # Nothing was written before the transient blip; retrying must not
            # drop the published CSM posture for a stale observation.
            return
        if not self.context.control_available():
            # Losing session qualification ends software ownership even when
            # the display rollback itself cannot finish.
            self._release_slate()
            return
        if (isinstance(error, DisplayError)
                and error.detail == 'convertible-slate-notification-failed'):
            # A partially attempted CSM write still needs the journaled
            # startup value restored. A display failure after a successful
            # notification keeps the current lease for the bounded retry.
            self._release_slate()

    def _native_handoff_active(self) -> bool:
        active = getattr(self.display, 'native_handoff_active', False)
        return bool(active() if callable(active) else active)

    def _verify_after_slate(self, target: Target) -> None:
        verify = getattr(self.display, 'verify_software_target', None)
        if verify is None:
            return
        try:
            verify(target)
        except (DisplayError, OSError, ValueError) as error:
            if (not isinstance(error, DisplayError)
                    or error.code not in (ErrorCode.DISPLAY_LOCK, ErrorCode.DISPLAY_TRANSFORM)):
                raise
            if error.detail == 'physical-tablet-took-over':
                # The display already handed ownership to Windows. Publishing
                # the older software target here would fight the physical
                # tablet before the next service generation is read.
                self._sync_slate_mode('hardware')
                return
            # Publishing the software CSM posture can make Windows finish a
            # native rotation after the initial CCD transaction. Reassert the
            # already-fixed target while the notification is unchanged; this
            # avoids a second CSM response while ensuring the lock and
            # direction that are reported ready are the final platform state.
            log.warning(
                'Windows display changed after slate notification; reasserting %s: %s',
                target.mode,
                error,
            )
            self._require_available()
            self.display.apply(target)
            verify(target)

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
            elif self.slate is not None and target.mode in ('tent', 'stand'):
                # Windows re-enables automatic rotation and moves the panel to
                # its native direction right after the slate posture is
                # published. Writing the fixed direction first provably caused
                # two visible rotations per transition on the device: our
                # write, Windows' response, then the bounded reassert.
                # Publish first, let that response settle, then own the
                # direction and lock with a single write.
                self._sync_slate(target)
                self._require_available()
                self.settle(SLATE_SETTLE_SECONDS)
                self._require_available()
                self.display.apply(target)
                self._verify_after_slate(target)
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
                native_handoff = self._native_handoff_active()
                if native_handoff:
                    self._sync_slate_mode('hardware')
                else:
                    self._sync_slate(target)
                if self.slate is not None and not native_handoff:
                    self._verify_after_slate(target)
            if not self.context.control_available():
                raise DisplayError(
                    ErrorCode.DISPLAY_UNAVAILABLE,
                    'session changed during display apply',
                )
        except BaseException as error:
            if not (
                isinstance(error, DisplayError)
                and error.detail == TRANSIENT_CONTROL_DETAIL
            ):
                self._restore_after_failed_transaction()
            self._release_slate_after_failure(error)
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
        except BaseException as error:
            if not (
                isinstance(error, DisplayError)
                and error.detail == TRANSIENT_CONTROL_DETAIL
            ):
                self._restore_after_failed_transaction()
            self._release_slate_after_failure(error)
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
