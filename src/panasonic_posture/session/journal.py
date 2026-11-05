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
from typing import Any

from ..protocol import SESSION_STATE_DIR_NAME

log = logging.getLogger(__name__)

JOURNAL_VERSION = 1
JOURNAL_NAME = "display.json"
_SPEC_FIELDS = 4

#: The journal is shared by all display backends.  ``backend`` was added when
#: Plasma support landed; documents written by 0.3.0 GNOME builds do not
#: contain it and are treated as GNOME journals.
BACKEND_SPEC_FIELDS = {"gnome": _SPEC_FIELDS, "plasma": 1}


def default_path() -> Path:
    """Return ``$XDG_STATE_HOME/panasonic-posture-manager/display.json``."""

    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return Path(base) / SESSION_STATE_DIR_NAME / JOURNAL_NAME


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate(data: Any) -> dict:
    """Validate a loaded journal document, raising ``ValueError`` if invalid."""

    if not isinstance(data, dict):
        raise ValueError("journal must be a JSON object")
    if data.get("version") != JOURNAL_VERSION:
        raise ValueError(f"unsupported journal version {data.get('version')!r}")
    backend = data.get("backend", "gnome")
    if not isinstance(backend, str) or not backend:
        raise ValueError("journal backend must be a non-empty string")
    expected_fields = BACKEND_SPEC_FIELDS.get(backend, 1)
    spec = data.get("spec")
    if not isinstance(spec, (list, tuple)) or len(spec) != expected_fields:
        raise ValueError(
            f"journal spec for {backend} must be a list of {expected_fields} strings"
        )
    if not all(isinstance(part, str) for part in spec):
        raise ValueError("journal spec must be a list of strings")
    if not _is_int(data.get("original_transform")):
        raise ValueError("journal original_transform must be an integer")
    if not isinstance(data.get("original_lock"), bool):
        raise ValueError("journal original_lock must be a boolean")
    for field in ("written_lock", "pending_lock"):
        lock = data.get(field)
        if lock is not None and not isinstance(lock, bool):
            raise ValueError(f"journal {field} must be a boolean or null")
    for field in ("written_transform", "pending_transform"):
        transform = data.get(field)
        if transform is not None and not _is_int(transform):
            raise ValueError(f"journal {field} must be an integer or null")
    original_policy = data.get("original_policy", None)
    if original_policy is not None and not isinstance(original_policy, str):
        raise ValueError("journal original_policy must be a string or null")
    return data


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
