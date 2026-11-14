"""Read-only checks required before starting user-facing hardware mode."""
import os


def require_native_user() -> None:
    from panasonic_posture.windows.common.security import WindowsSecurity

    security = WindowsSecurity()
    security.require_unprivileged()
    identity = security.process_identity(os.getpid())
    console = security.kernel.WTSGetActiveConsoleSessionId()
    if console in (0, 0xffffffff) or identity.session_id != console:
        raise PermissionError('native screen control requires the local console session')


__all__ = ['require_native_user']
