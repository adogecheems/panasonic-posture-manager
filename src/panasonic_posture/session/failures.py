"""Pure session failures, without I/O or transport-specific decisions."""

from enum import StrEnum

from ..contracts import ErrorCode


def describe_error(error: BaseException) -> str:
    """Best-effort message for a ``GLib.Error`` without calling ``str()``.

    PyGObject's ``GLib.Error.__str__`` can itself raise for D-Bus remote
    errors, so logging must never rely on it.
    """

    message = getattr(error, "message", None)
    if isinstance(message, str) and message:
        return message
    try:
        return str(error)
    except Exception:  # pragma: no cover - defensive
        return error.__class__.__name__


class SessionError(Exception):
    """Base class for session layer failures with a stable protocol code."""

    def __init__(self, code: str = ErrorCode.INTERNAL, detail: str | None = None):
        self.code = str(code or ErrorCode.INTERNAL)
        self.detail = detail or None
        super().__init__(self.code if not self.detail else f"{self.code}: {self.detail}")

    def __str__(self) -> str:  # pragma: no cover - inherited formatting is fine
        return super().__str__()


class FailureKind(StrEnum):
    REJECTED = "rejected"
    TRANSIENT = "transient"
    UNAVAILABLE = "unavailable"


class ServiceError(SessionError):
    """A classified service failure; platform details are for diagnostics only."""

    def __init__(
        self,
        code: str = ErrorCode.INTERNAL,
        detail: str | None = None,
        *,
        kind: FailureKind | None = None,
        name: str = "",
    ) -> None:
        # A known denial cannot become retryable through a contradictory hint.
        self.kind = (
            FailureKind.REJECTED if code == ErrorCode.ACCESS_DENIED
            else FailureKind(kind) if kind is not None else FailureKind.TRANSIENT
        )
        self.name = name or ""
        super().__init__(code, detail)


class DisplayError(SessionError, ValueError):
    """A display transaction failed."""
