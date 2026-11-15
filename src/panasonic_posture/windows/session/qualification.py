"""Qualification fence around the authenticated native-session RPC client."""
import logging
import time
from typing import Protocol

from panasonic_posture.contracts import ErrorCode
from panasonic_posture.model import StateSnapshot
from panasonic_posture.session.failures import FailureKind, ServiceError
from panasonic_posture.session.ports import SessionClient
from panasonic_posture.windows.session.display import SessionContext, transient_context_blip

log = logging.getLogger(__name__)


class ClosableSessionClient(SessionClient, Protocol):
    def close(self) -> None: ...


class ReleaseAwareSessionClient(ClosableSessionClient, Protocol):
    def hardware_release(self) -> tuple[StateSnapshot, str | None]: ...


def unavailable_session_error(detail: str = 'native-session-unavailable') -> ServiceError:
    return ServiceError(
        ErrorCode.DISPLAY_UNAVAILABLE,
        detail,
        kind=FailureKind.REJECTED,
    )


class QualifiedSessionClient:
    """Reject display-adjacent work as soon as local qualification is lost."""

    def __init__(self, client: ClosableSessionClient, context: SessionContext, *,
                 clock=time.monotonic) -> None:
        self.client, self.context = client, context
        self.clock = clock
        self._last_unavailable_log = float('-inf')

    @staticmethod
    def _context_blip(context: SessionContext) -> bool:
        return transient_context_blip(context)

    def _rejected(self, detail: str = 'native session control is unavailable') -> ServiceError:
        kind = (
            FailureKind.UNAVAILABLE
            if self._context_blip(self.context)
            else FailureKind.REJECTED
        )
        return ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, detail, kind=kind)

    def _require_available(self) -> None:
        if not self.context.control_available():
            self._log_unavailable_once()
            raise self._rejected()

    def _log_unavailable_once(self) -> None:
        if self.clock() - self._last_unavailable_log >= 2.0:
            self._last_unavailable_log = self.clock()
            snapshot = getattr(self.context, 'snapshot', None)
            log.warning(
                'native session control is unavailable: %s',
                snapshot() if callable(snapshot) else 'no context snapshot',
            )

    def _report_lost(self, generation: int) -> None:
        self.client.session_update(
            generation,
            False,
            ErrorCode.DISPLAY_UNAVAILABLE,
            'native-session-unavailable',
        )

    def get_state(self) -> StateSnapshot:
        state = self.client.get_state()
        if not self.context.control_available():
            self._log_unavailable_once()
            if not self._context_blip(self.context):
                self._report_lost(state.generation)
            raise self._rejected()
        return state

    def get_recovery_state(self) -> StateSnapshot:
        """Keep physical recovery queries available after local lock loss.

        The service still authenticates this process and limits the locked
        connection to reads. Apply and receipts retain their qualification fence.
        """
        return self.client.get_state()

    def session_update(self, generation: int, ready: bool,
                       error_code: str | None = None,
                       error_detail: str | None = None) -> None:
        if not self.context.control_available():
            self._log_unavailable_once()
            if not self._context_blip(self.context):
                self._report_lost(generation)
            raise self._rejected()
        self.client.session_update(generation, ready, error_code, error_detail)
        self._require_available()

    def confirm_tent(self, epoch: int) -> None:
        self._require_available()
        self.client.confirm_tent(epoch)

    def confirm_stand(self, epoch: int) -> None:
        self._require_available()
        self.client.confirm_stand(epoch)

    def wake_screen(self, generation: int, serial: int) -> None:
        self._require_available()
        self.client.wake_screen(generation, serial)


__all__ = [
    'ClosableSessionClient', 'QualifiedSessionClient', 'ReleaseAwareSessionClient',
    'unavailable_session_error',
]
