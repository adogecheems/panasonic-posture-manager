"""Linux display-journal location and compatibility exports."""

from __future__ import annotations

import os
from pathlib import Path

from ..linux_contract import SESSION_STATE_DIR_NAME
from .journal_store import (
    BACKEND_SPEC_FIELDS as BACKEND_SPEC_FIELDS,
    JOURNAL_NAME as JOURNAL_NAME,
    JOURNAL_VERSION as JOURNAL_VERSION,
    Journal as Journal,
)


def default_path() -> Path:
    """Return ``$XDG_STATE_HOME/panasonic-posture-manager/display.json``."""

    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / SESSION_STATE_DIR_NAME / JOURNAL_NAME


__all__ = ["JOURNAL_NAME", "JOURNAL_VERSION", "Journal", "default_path"]
