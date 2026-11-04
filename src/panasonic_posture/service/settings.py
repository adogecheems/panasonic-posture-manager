"""Persistent service settings with atomic, crash-safe writes.

Only a very small amount of user preference survives a service restart.  The
file is written atomically (temporary file + fsync + ``os.replace``) with mode
0600.  A file that cannot be parsed, or that does not match the expected schema,
is quarantined as ``<name>.corrupt-<timestamp>`` and default settings are used
instead of letting the daemon crash on every start.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..calibration import parse_calibration_matrix
from ..protocol import STATE_DIR

LOG = logging.getLogger(__name__)

#: Schema version of the JSON file written by :class:`SettingsStore`.
SETTINGS_VERSION = 1

DEFAULT_SETTINGS_PATH = Path(STATE_DIR) / "settings.json"


@dataclass(frozen=True)
class Settings:
    """User preferences owned by the system service."""

    tent_enabled: bool = True
    tent_prompt: bool = True
    stand_prompt: bool = False
    sensor_calibration: tuple[float, ...] | None = None


class SettingsStore:
    """Load and store :class:`Settings` at ``path``.

    The store never raises on load: unreadable or invalid files are logged and
    replaced by defaults.  ``save`` may raise :class:`OSError` so callers can
    roll back an in-memory change and report a stable error code.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        time_source: Callable[[], float] = time.time,
        logger: logging.Logger = LOG,
    ) -> None:
        self.path = Path(path)
        self._time = time_source
        self._log = logger

    # -- reading ---------------------------------------------------------

    def load(self) -> Settings:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return Settings()
        except OSError as error:
            self._log.warning("cannot read settings %s: %s", self.path, error)
            return Settings()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            # A file that is not even UTF-8 is corruption like any other bad
            # schema: isolate it instead of raising on every service start.
            self._quarantine(f"invalid UTF-8: {error}")
            return Settings()
        try:
            data = json.loads(text)
        except ValueError as error:
            self._quarantine(f"invalid JSON: {error}")
            return Settings()
        try:
            return self._parse(data)
        except ValueError as error:
            self._quarantine(str(error))
            return Settings()

    def _parse(self, data: object) -> Settings:
        if not isinstance(data, dict):
            raise ValueError("settings must be a JSON object")
        version = data.get("version")
        if version != SETTINGS_VERSION:
            raise ValueError(f"unsupported settings version {version!r}")
        tent_enabled = data.get("tent_enabled", True)
        if not isinstance(tent_enabled, bool):
            raise ValueError("tent_enabled must be a boolean")
        tent_prompt = data.get("tent_prompt")
        if not isinstance(tent_prompt, bool):
            raise ValueError("tent_prompt must be a boolean")
        stand_prompt = data.get("stand_prompt", False)
        if not isinstance(stand_prompt, bool):
            raise ValueError("stand_prompt must be a boolean")
        try:
            sensor_calibration = parse_calibration_matrix(
                data.get("sensor_calibration")
            )
        except ValueError as error:
            raise ValueError(f"invalid sensor_calibration: {error}") from error
        return Settings(
            tent_enabled=tent_enabled,
            tent_prompt=tent_prompt,
            stand_prompt=stand_prompt,
            sensor_calibration=sensor_calibration,
        )

    # -- writing ---------------------------------------------------------

    def save(self, settings: Settings) -> None:
        """Atomically replace the settings file with ``settings``."""
        if not isinstance(settings, Settings):
            raise TypeError("settings must be a Settings instance")
        payload = {
            "version": SETTINGS_VERSION,
            "tent_enabled": bool(settings.tent_enabled),
            "tent_prompt": bool(settings.tent_prompt),
            "stand_prompt": bool(settings.stand_prompt),
            "sensor_calibration": (
                list(settings.sensor_calibration)
                if settings.sensor_calibration is not None
                else None
            ),
        }
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.tmp-", dir=directory
            )
            temporary = Path(name)
            try:
                os.fchmod(descriptor, 0o600)
            except BaseException:
                os.close(descriptor)
                temporary.unlink(missing_ok=True)
                raise
            # ``os.fdopen`` owns the descriptor from now on; the ``with`` block
            # closes it on success *and* failure.
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            self._fsync_directory(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    # -- corruption handling --------------------------------------------

    def _quarantine(self, reason: str) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime(self._time()))
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        counter = 1
        while target.exists():
            target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{counter}")
            counter += 1
        try:
            os.replace(self.path, target)
        except OSError as error:
            self._log.error("cannot quarantine corrupt settings %s: %s", self.path, error)
            return
        self._log.warning(
            "quarantined corrupt settings %s -> %s (%s)", self.path, target, reason
        )
