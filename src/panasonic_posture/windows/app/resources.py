"""Windows source and installed resources and gettext without Linux configuration."""
import ast
import ctypes
import gettext
import os
from pathlib import Path

from ..common.layout import distribution_root
from ...localization import (
    SIMPLIFIED_CHINESE_CATALOG,
    catalog_language,
    catalog_languages,
    environment_languages,
)
from ..app.localization import windows_catalog_language


def data_root() -> Path:
    override = os.environ.get('PANASONIC_POSTURE_DATA_DIR')
    return Path(override) if override else distribution_root(__file__) / 'data'


def asset_root() -> Path:
    """Return the Windows asset directory for source or installed layouts."""

    base = data_root()
    source = base / 'assets' / 'windows'
    return source if source.is_dir() else base / 'assets'


def _windows_language() -> str | None:
    if os.name != 'nt':
        return None
    try:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        function = kernel.GetUserDefaultUILanguage
        function.restype = ctypes.c_uint16
        function.argtypes = []
        langid = int(function())
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    if langid <= 0:
        return None
    # Use the Windows UI language rather than process LANG=C.UTF-8. Traditional
    # Chinese intentionally falls back to English because only zh_CN ships.
    return windows_catalog_language(langid)


def default_language() -> str:
    system_language = _windows_language()
    if system_language:
        return system_language
    languages = environment_languages()
    return languages[0] if languages else 'en'


def translator(language: str | None = None):
    language = language or default_language()
    root = data_root().parent
    languages = catalog_languages((language,))
    compiled = gettext.translation('panasonic-posture-manager', localedir=root / 'locale',
                                   languages=languages, fallback=True)
    if catalog_language(language) != SIMPLIFIED_CHINESE_CATALOG:
        return compiled.gettext
    # Windows source runs do not require the GNU msgfmt executable.
    # The project catalog contains singular entries only. Installed builds can
    # use the compiled catalog above; no second translation dictionary is kept.
    catalog = root / 'po' / 'zh_CN.po'
    if not catalog.is_file():
        return compiled.gettext
    messages = {}
    key, value, part = '', '', None
    for line in catalog.read_text(encoding='utf-8').splitlines() + ['']:
        line = line.strip()
        if not line:
            if key and value:
                messages[key] = value
            key, value, part = '', '', None
        elif line.startswith('msgid '):
            key, part = ast.literal_eval(line[6:]), 'key'
        elif line.startswith('msgstr '):
            value, part = ast.literal_eval(line[7:]), 'value'
        elif line.startswith('"'):
            if part == 'key':
                key += ast.literal_eval(line)
            elif part == 'value':
                value += ast.literal_eval(line)
    def translate(text):
        value = compiled.gettext(text)
        # A source checkout can contain yesterday's generated .mo. Keep its
        # entries but resolve newly added strings from the source catalog.
        return messages.get(text, text) if value == text else value
    return translate


def application_name(language: str | None = None) -> str:
    """Return the same user-facing name used by the native window title."""
    _ = translator(language)
    return _('Panasonic 2-in-1 Tablet Posture Management Tool')
