"""Durable display-ownership journal.

The journal records what was changed on the user session before the change is
made.  If the adapter crashes, restarts or the session is killed, the next
startup can restore the previous rotation/lock from this file.  Two
properties matter:

* the write is atomic and durable (temporary file, fsync file and directory,
  mode 0600, ``os.replace``);
* a corrupt or schema-invalid journal never crashes the session service: it
  is renamed to ``<name>.corrupt-<ts>`` and treated as absent.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from ..linux_contract import SESSION_STATE_DIR_NAME
from .journal_schema import (
    JOURNAL_VERSION as JOURNAL_VERSION,
    BACKEND_SPEC_FIELDS as BACKEND_SPEC_FIELDS,
    validate as _validate,
)

log = logging.getLogger(__name__)

JOURNAL_NAME = "display.json"


def default_path() -> Path:
    """Return ``$XDG_STATE_HOME/panasonic-posture-manager/display.json``."""

    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / SESSION_STATE_DIR_NAME / JOURNAL_NAME


class Journal:
    """Read/write the display journal at *path*.  Pure standard library."""

    def __init__(self, path: str | os.PathLike[str], clock=time.time):
        self.path = Path(path)
        self._clock = clock

    # -- reading ----------------------------------------------------------

    def load(self) -> dict | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError as error:
            self._quarantine(error)
            return None
        except IsADirectoryError:
            log.warning("display journal %s is a directory; ignoring", self.path)
            return None
        except OSError as error:
            log.warning("cannot read display journal %s: %s", self.path, error)
            return None
        try:
            document = json.loads(raw)
            return _validate(document)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            self._quarantine(error)
            return None

    # -- writing ----------------------------------------------------------

    def save(self, document: dict) -> None:
        document = _validate(document)
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:  # pragma: no cover - odd filesystems
            pass
        payload = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            _fsync_directory(directory)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            log.warning("cannot remove display journal %s: %s", self.path, error)
            return
        _fsync_directory(self.path.parent)

    # -- internals --------------------------------------------------------

    def _quarantine(self, reason: BaseException | str) -> None:
        stamp = int(self._clock())
        target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        counter = 0
        while target.exists():
            counter += 1
            target = self.path.with_name(f"{self.path.name}.corrupt-{stamp}-{counter}")
        try:
            self.path.replace(target)
            log.warning("display journal %s was invalid (%s); moved to %s", self.path, reason, target.name)
        except OSError as error:  # pragma: no cover - best effort quarantine
            log.warning("display journal %s is invalid (%s): %s", self.path, reason, error)


def _fsync_directory(directory: Path) -> None:
    """Best-effort fsync of a directory so a rename survives a crash."""

    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:  # pragma: no cover - portable fallback
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - some filesystems refuse directory fsync
        pass
    finally:
        os.close(descriptor)


__all__ = ["JOURNAL_NAME", "JOURNAL_VERSION", "Journal", "default_path"]
