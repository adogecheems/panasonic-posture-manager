"""Bounded per-user diagnostics for the private Windows backend."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_BYTES = 256 * 1024
LOG_BACKUPS = 1


def log_path(settings: str | Path) -> Path:
    """Keep diagnostics beside the selected settings without a shared name."""
    path = Path(settings)
    return path.with_suffix('.log')


def create_handler(settings: str | Path, *, limit: int = LOG_BYTES) -> RotatingFileHandler:
    path = log_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    return RotatingFileHandler(
        path,
        maxBytes=limit,
        backupCount=LOG_BACKUPS,
        encoding='utf-8',
        delay=True,
    )


def configure_logging(settings: str | Path) -> None:
    """Use the bounded log, but never prevent the backend from starting."""
    try:
        handler = create_handler(settings)
    except OSError:
        logging.basicConfig(stream=None, level=logging.WARNING)
        return
    logging.basicConfig(
        handlers=[handler],
        level=logging.WARNING,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )
