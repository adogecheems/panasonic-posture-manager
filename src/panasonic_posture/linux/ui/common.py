"""Shared translations and resource discovery."""
import os
from pathlib import Path
from ..session.errors import get_translator
from ..contracts import DATA_DIR

_ = get_translator()


def data_root() -> Path:
    override = os.environ.get('PANASONIC_POSTURE_UI_DIR')
    if override:
        return Path(override).parent / 'assets'
    source = Path(__file__).resolve().parents[3] / 'data'
    source_assets = source / 'assets' / 'linux'
    return source_assets if source_assets.is_dir() else Path(DATA_DIR) / 'assets'


DATA_ROOT = data_root()
