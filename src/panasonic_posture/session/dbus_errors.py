"""Translate Linux transport failures at the client boundary."""

from .failures import ErrorCode, FailureKind, ServiceError

#: D-Bus error names that mean "identity/lease rejected", not a transient
#: transport failure.  They must stop display transactions immediately (P3).
AUTH_ERROR_NAMES = frozenset(
    {
        "org.freedesktop.DBus.Error.AccessDenied",
        "org.freedesktop.DBus.Error.AuthFailed",
        "org.freedesktop.DBus.Error.NotAuthorized",
    }
)

#: Error detail markers used by the service for the same class of rejection.
AUTH_ERROR_DETAIL_MARKERS = (
    "access-denied",
    "not-session-adapter",
    "session-lease-busy",
)


def service_error(code: str, detail: str | None = None, *, name: str = "") -> ServiceError:
    rejected = (
        code == ErrorCode.ACCESS_DENIED
        or name in AUTH_ERROR_NAMES
        or any(marker in (detail or "") for marker in AUTH_ERROR_DETAIL_MARKERS)
    )
    kind = FailureKind.REJECTED if rejected else FailureKind.TRANSIENT
    if not rejected and name == "org.freedesktop.DBus.Error.ServiceUnknown":
        kind = FailureKind.UNAVAILABLE
    return ServiceError(code, detail, kind=kind, name=name)
