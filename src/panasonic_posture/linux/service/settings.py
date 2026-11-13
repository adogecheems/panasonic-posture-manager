"""Linux settings location and compatible exports of the shared atomic store."""
from pathlib import Path
import os  # Compatible module export used by existing fault-injection callers.

from ..contracts import STATE_DIR
from ...service.settings_store import SettingsStore as SettingsStore
from ...service.settings_schema import SETTINGS_VERSION as SETTINGS_VERSION
from ...service.ports import Settings as Settings

DEFAULT_SETTINGS_PATH = Path(STATE_DIR) / "settings.json"
