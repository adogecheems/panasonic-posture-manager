"""Portable source-preview resources and gettext without Linux configuration."""
import ast
import ctypes
import gettext
import locale
import os
from pathlib import Path


def data_root() -> Path:
    override = os.environ.get('PANASONIC_POSTURE_DATA_DIR')
    return Path(override) if override else Path(__file__).resolve().parents[3] / 'data'


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
    # The project ships one Chinese catalog. Use it for every Chinese
    # Windows UI language rather than falling back to process LANG=C.UTF-8.
    return 'zh_CN' if langid & 0x3ff == 0x04 else 'en'


def default_language() -> str:
    system_language = _windows_language()
    if system_language:
        return system_language
    return (os.environ.get('LANGUAGE', '').split(':')[0] or
            os.environ.get('LC_ALL', '') or
            os.environ.get('LC_MESSAGES', '') or
            os.environ.get('LANG', '') or
            locale.getlocale()[0] or
            'en')


def translator(language: str | None = None):
    language = language or default_language()
    root = data_root().parent
    compiled = gettext.translation('panasonic-posture-manager', localedir=root / 'locale',
                                   languages=[language], fallback=True)
    if not language.lower().startswith('zh'):
        return compiled.gettext
    # Source previews on Windows do not require the GNU msgfmt executable.
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
