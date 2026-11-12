"""Default-on current-user login startup; never installs a service or elevates."""
from collections.abc import Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
import subprocess
import sys
from typing import Protocol

RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_NAME = 'PanasonicPostureManager'
PREFERENCE_KEY = r'Software\PanasonicPostureManager'
PREFERENCE_NAME = 'LoginStartupInitialized'
log = logging.getLogger(__name__)


class StartupError(OSError):
    pass


class StartupRegistry(Protocol):
    def read(self) -> str | None: ...
    def write(self, command: str) -> None: ...
    def delete(self) -> None: ...
    def read_initialized(self) -> bool: ...
    def mark_initialized(self) -> None: ...


class DefaultStartupInitializer(Protocol):
    def ensure_default_enabled(self) -> None: ...


class NativeRunRegistry:
    def read(self) -> str | None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, kind = winreg.QueryValueEx(key, RUN_NAME)
        except FileNotFoundError:
            return None
        if kind != winreg.REG_SZ or not isinstance(value, str):
            raise StartupError('startup-entry-conflict')
        return value

    def write(self, command: str) -> None:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_NAME)
        except FileNotFoundError:
            pass

    def read_initialized(self) -> bool:
        import winreg
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, PREFERENCE_KEY, 0, winreg.KEY_QUERY_VALUE
            ) as key:
                value, kind = winreg.QueryValueEx(key, PREFERENCE_NAME)
        except FileNotFoundError:
            return False
        return kind == winreg.REG_DWORD and value == 1

    def mark_initialized(self) -> None:
        import winreg
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, PREFERENCE_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, PREFERENCE_NAME, 0, winreg.REG_DWORD, 1)


@dataclass(frozen=True)
class StartupStatus:
    enabled: bool
    available: bool
    conflict: bool = False


class LoginStartup:
    def __init__(self, command: Sequence[str], *, registry: StartupRegistry | None = None) -> None:
        if (isinstance(command, (str, bytes)) or not command or
                any(not isinstance(value, str) or not value or '\0' in value for value in command)
                or not Path(command[0]).is_absolute()):
            raise ValueError('an absolute fixed startup command is required')
        self.command = subprocess.list2cmdline(command)
        # HKCU Run has the documented 260-character command limit. Count UTF-16
        # units, rather than accepting a Python-length string Windows truncates.
        if len(self.command.encode('utf-16-le')) // 2 > 260:
            raise StartupError('startup-command-too-long')
        self.registry = registry if registry is not None else NativeRunRegistry()

    def status(self) -> StartupStatus:
        value = self.registry.read()
        return StartupStatus(value == self.command, value in (None, self.command),
                             value not in (None, self.command))

    def ensure_default_enabled(self) -> None:
        """Register the first-launch default without overriding an explicit choice."""
        if self.registry.read_initialized():
            return
        value = self.registry.read()
        if value not in (None, self.command):
            raise StartupError('startup-entry-conflict')
        if value is None:
            self.registry.write(self.command)
            if self.registry.read() != self.command:
                raise StartupError('startup-change-not-applied')
        self._mark_initialized()

    def set_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise ValueError('startup preference must be a boolean')
        value = self.registry.read()
        if value not in (None, self.command):
            raise StartupError('startup-entry-conflict')
        desired = self.command if enabled else None
        # Persist the explicit choice first so a later launch cannot mistake a
        # disabled value for an untouched first-run default.
        self._mark_initialized()
        if value == desired:
            return
        if enabled:
            self.registry.write(self.command)
        else:
            self.registry.delete()
        if self.registry.read() != desired:
            raise StartupError('startup-change-not-applied')

    def _mark_initialized(self) -> None:
        if self.registry.read_initialized():
            return
        self.registry.mark_initialized()
        if not self.registry.read_initialized():
            raise StartupError('startup-change-not-applied')


class UnavailableStartup:
    def status(self) -> StartupStatus:
        return StartupStatus(False, False)

    def ensure_default_enabled(self) -> None:
        pass

    def set_enabled(self, enabled: bool) -> None:
        raise StartupError('startup-location-unavailable')


def initialize_default_startup(startup: DefaultStartupInitializer) -> None:
    """Apply the first-run default without making UI startup depend on registry health."""
    try:
        startup.ensure_default_enabled()
    except (OSError, ValueError) as error:
        log.warning('cannot initialize login startup preference: %s', error)


def startup_command(*, executable: Path | None = None, root: Path | None = None) -> tuple[str, ...]:
    """Use only fixed project entry points; login has no source PYTHONPATH/cwd."""
    executable = Path(sys.executable) if executable is None else executable
    root = Path(__file__).resolve().parents[3] if root is None else root
    if not executable.is_absolute() or not root.is_absolute():
        raise StartupError('startup-location-unavailable')
    pythonw = executable.with_name('pythonw.exe')
    if not pythonw.is_file():
        raise StartupError('startup-windowless-runtime-unavailable')
    if (root / 'launch.py').is_file():
        return str(pythonw), '-I', str(root / 'launch.py'), '--background'
    source = root / 'packaging/windows/launch-hardware.py'
    if not source.is_file() or not (root / 'src/panasonic_posture').is_dir():
        raise StartupError('startup-source-entry-unavailable')
    return str(pythonw), str(source)
