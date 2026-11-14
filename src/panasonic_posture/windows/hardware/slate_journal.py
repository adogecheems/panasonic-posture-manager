"""Atomic crash-recovery journal for the convertible-slate notification lease."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import tempfile
import time

VERSION = 1
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlateLease:
    original: bool
    desired: bool


class SlateJournal:
    """Persist the startup slate value before changing the GPIO indicator."""

    def __init__(self, path: str | os.PathLike[str], clock=time.time) -> None:
        self.path = Path(path)
        self._clock = clock

    def load(self, *, quarantine: bool = True) -> SlateLease | None:
        try:
            document = json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (ValueError, RecursionError) as error:
            if quarantine:
                self._quarantine(error)
            return None
        try:
            if not isinstance(document, dict) or set(document) != {
                    'version', 'original', 'desired'}:
                raise ValueError('unexpected slate journal fields')
            if type(document['version']) is not int or document['version'] != VERSION:
                raise ValueError(f"unsupported slate journal version {document['version']!r}")
            original, desired = document['original'], document['desired']
            if type(original) is not bool or type(desired) is not bool:
                raise ValueError('slate journal states must be booleans')
        except ValueError as error:
            if quarantine:
                self._quarantine(error)
            return None
        return SlateLease(original, desired)

    def save(self, original: bool, desired: bool) -> None:
        if type(original) is not bool or type(desired) is not bool:
            raise ValueError('slate journal states must be booleans')
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        payload = json.dumps(
            {'version': VERSION, 'original': original, 'desired': desired},
            separators=(',', ':'),
        )
        descriptor, temporary = tempfile.mkstemp(
            prefix=f'.{self.path.name}.', suffix='.tmp', dir=directory
        )
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            self._fsync_directory()
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
            log.warning('cannot remove slate journal %s: %s', self.path, error)
            raise
        self._fsync_directory()

    def _quarantine(self, reason: BaseException | str) -> None:
        stamp = int(self._clock())
        target = self.path.with_name(f'{self.path.name}.corrupt-{stamp}')
        counter = 0
        while target.exists():
            counter += 1
            target = self.path.with_name(f'{self.path.name}.corrupt-{stamp}-{counter}')
        try:
            self.path.replace(target)
            log.warning(
                'slate journal %s was invalid (%s); moved to %s',
                self.path, reason, target.name,
            )
        except OSError as error:
            log.warning('slate journal %s is invalid (%s): %s', self.path, reason, error)
            raise

    def _fsync_directory(self) -> None:
        try:
            descriptor = os.open(
                self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)
            )
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


def lease_path(display_journal_path: str | os.PathLike[str]) -> Path:
    display = Path(display_journal_path)
    return display.with_name(f'{display.stem}-slate{display.suffix}')


__all__ = ['SlateJournal', 'SlateLease', 'VERSION', 'lease_path']
