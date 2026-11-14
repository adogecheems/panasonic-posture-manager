"""Fast Windows startup shell with lazily loaded Fluent Widgets content."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

from ..app.resources import application_name, asset_root, data_root


def local_state_directory() -> Path:
    """Return the per-user application state directory without side effects."""
    root = os.environ.get('LOCALAPPDATA')
    if not root or not Path(root).is_absolute():
        raise OSError('LOCALAPPDATA is not an absolute directory')
    return Path(root) / 'PanasonicPostureManager'


def _require_ui_dependencies() -> None:
    """Fail clearly before a tray shell starts without a window implementation."""
    for name in ('qfluentwidgets', 'qframelesswindow'):
        if importlib.util.find_spec(name) is None:
            raise ModuleNotFoundError(f'No module named {name!r}', name=name)


def _configure_application(app, language) -> None:
    name = application_name(language)
    app.setStyle('Fusion')
    app.setApplicationName(name)
    app.setApplicationDisplayName(name)


def _window_factory(*, language, client, startup):
    def create_window():
        from .app import Window, configure_application
        from qfluentwidgets import Theme, setTheme, setThemeColor

        configure_application(language)
        setTheme(Theme.AUTO)
        setThemeColor('#0051f2')
        return Window(
            language=language,
            client=client,
            startup=startup,
            start_client=False,
        )

    return create_window


def _show_start_failure(language) -> int:
    # The heavyweight module is needed only for its localized fatal dialog.
    from .app import hardware_start_failure
    return hardware_start_failure(language, 'session')


def run(*, language=None, background=False) -> int:
    """Start the native tray first and build the full window on first use."""
    activation = None
    startup = None
    resident = None
    hardware_ui = sys.platform == 'win32'
    if hardware_ui:
        from ..app.identity import configure_process_identity
        configure_process_identity()
        from ..common.native_preflight import require_native_user
        try:
            require_native_user()
        except (PermissionError, OSError):
            return _show_start_failure(language)
        from ..app.activation import WindowActivation
        from ..app.startup import (
            LoginStartup,
            UnavailableStartup,
            initialize_default_startup,
            startup_command,
        )
        try:
            activation = WindowActivation(local_state_directory())
        except OSError:
            return _show_start_failure(language)
        if not activation.acquire(background=background):
            activation.close()
            activation = None
            return 0
        try:
            startup = LoginStartup(startup_command())
        except (OSError, ValueError):
            startup = UnavailableStartup()
        else:
            initialize_default_startup(startup)

    try:
        _require_ui_dependencies()
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication

        from .resident import ResidentTray
        from .unavailable import UnavailableClient
        if hardware_ui:
            from ..ipc.client import HardwareUIClient
            client = HardwareUIClient()
        else:
            client = UnavailableClient()

        app = QApplication([sys.argv[0]])
        _configure_application(app, language)
        app.setWindowIcon(QIcon(str(
            asset_root() / 'work.mmoe.panasonic.PostureManager.png'
        )))
        if hardware_ui:
            from ..app.identity import APP_USER_MODEL_ID
            app.setDesktopFileName(APP_USER_MODEL_ID)

        resident = ResidentTray(
            None,
            window_factory=_window_factory(
                language=language,
                client=client,
                startup=startup,
            ),
            client=client,
            activation=activation,
            language=language,
        )
        if not background or not resident.enabled:
            resident.show_window()
        return app.exec()
    finally:
        if resident is not None:
            resident.shutdown()
            window = resident.window
            if window is not None:
                from PyQt6 import sip
                from PyQt6.QtCore import QCoreApplication, QEvent
                if not sip.isdeleted(window):
                    window.close()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if activation is not None:
            activation.close()


__all__ = ['local_state_directory', 'run']
