"""Stable Windows shell identity for the desktop and taskbar icon."""
from __future__ import annotations

import ctypes
from collections.abc import Callable
import sys


APP_USER_MODEL_ID = "work.mmoe.panasonic.PostureManager"


def configure_process_identity(
    *,
    platform: str | None = None,
    setter: Callable[[str], int] | None = None,
) -> bool:
    """Assign the packaged app identity before Qt creates native windows.

    Failure is deliberately non-fatal: device state and control do not depend
    on shell presentation. The caller still sets the window icon from the
    bundled image, while this identity prevents taskbar grouping from falling
    back to the shared ``pythonw.exe`` identity.
    """

    current_platform = sys.platform if platform is None else platform
    if setter is None:
        if current_platform != "win32":
            return False
        try:
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            setter = shell32.SetCurrentProcessExplicitAppUserModelID
            setter.argtypes = [ctypes.c_wchar_p]
            setter.restype = ctypes.c_long
        except (AttributeError, OSError):
            return False
    try:
        return int(setter(APP_USER_MODEL_ID)) >= 0
    except (OSError, ValueError, TypeError):
        return False


__all__ = ["APP_USER_MODEL_ID", "configure_process_identity"]
