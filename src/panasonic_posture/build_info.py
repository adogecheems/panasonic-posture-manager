"""Display-only version identity for installation-test builds."""
from pathlib import Path
import re

from . import __version__


_BUILD_FILE = Path(__file__).with_name('BUILD-VERSION')


def display_version(raw: str | None = None) -> str:
    """Return the release version or its packaged test-build label."""

    if raw is None:
        try:
            raw = _BUILD_FILE.read_text(encoding='ascii').strip()
        except (OSError, UnicodeError):
            return __version__
    pattern = rf'{re.escape(__version__)}-build[1-9][0-9]*'
    return raw if isinstance(raw, str) and re.fullmatch(pattern, raw) else __version__


__all__ = ['display_version']
