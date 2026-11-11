"""Qualification fence around the authenticated native-session RPC client."""
from typing import Protocol

from ..contracts import ErrorCode
from ..model import StateSnapshot
from ..session.failures import FailureKind, ServiceError
from ..session.ports import SessionClient
from .session_display import SessionContext


class ClosableSessionClient(SessionClient, Protocol):
    def close(self) -> None: ...


def unavailable_session_error(detail: str = 'native-session-unavailable') -> ServiceError:
    return ServiceError(
        ErrorCode.DISPLAY_UNAVAILABLE,
        detail,
        kind=FailureKind.REJECTED,
    )


class QualifiedSessionClient:
    """Reject display-adjacent work as soon as local qualification is lost."""

    def __init__(self, client: ClosableSessionClient, context: SessionContext) -> None:
        self.client, self.context = client, context

    @staticmethod
    def _rejected(detail: str = 'native session control is unavailable') -> ServiceError:
        return ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, detail, kind=FailureKind.REJECTED)

    def _require_available(self) -> None:
        if not self.context.control_available():
            raise self._rejected()

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
            self._report_lost(state.generation)
            raise self._rejected()
        return state

    def session_update(self, generation: int, ready: bool,
                       error_code: str | None = None,
                       error_detail: str | None = None) -> None:
        if not self.context.control_available():
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
    'ClosableSessionClient', 'QualifiedSessionClient', 'unavailable_session_error',
]
