"""Shared translations and resource discovery."""
import os
from pathlib import Path
from ..session.errors import get_translator
from ..linux_contract import DATA_DIR

_ = get_translator()


def data_root() -> Path:
    override = os.environ.get('PANASONIC_POSTURE_UI_DIR')
    if override:
        return Path(override).parent
    source = Path(__file__).resolve().parents[3] / 'data'
    return source if (source / 'assets').is_dir() else Path(DATA_DIR)


DATA_ROOT = data_root()
