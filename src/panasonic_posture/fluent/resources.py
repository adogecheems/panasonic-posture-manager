"""Portable source-preview resources and gettext without Linux configuration."""
import ast
import gettext
import locale
import os
from pathlib import Path


def data_root() -> Path:
    override = os.environ.get('PANASONIC_POSTURE_DATA_DIR')
    return Path(override) if override else Path(__file__).resolve().parents[3] / 'data'


def translator(language: str | None = None):
    language = language or os.environ.get('LANGUAGE', '').split(':')[0] or locale.getlocale()[0] or 'en'
    root = data_root().parent
    compiled = gettext.translation('panasonic-posture-manager', localedir=root / 'locale',
                                   languages=[language], fallback=True)
    if isinstance(compiled, gettext.GNUTranslations) or not language.lower().startswith('zh'):
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
    return lambda text: messages.get(text, text)
