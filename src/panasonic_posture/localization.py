"""Shared language selection for the two supported application languages."""

from __future__ import annotations

import locale
import os
from collections.abc import Mapping, Sequence

SIMPLIFIED_CHINESE_CATALOG = "zh_CN"
_TRADITIONAL_CHINESE_REGIONS = frozenset({"hk", "mo", "tw"})


def catalog_language(tag: str | None) -> str | None:
    """Return the shipped catalog for *tag*, or ``None`` for English fallback.

    The project ships one Chinese catalog written in Simplified Chinese.  A
    Traditional Chinese UI therefore falls back to English rather than showing
    different Chinese text from a different orthography.
    """

    if not isinstance(tag, str) or not tag.strip():
        return None
    cleaned = tag.strip().split(".", 1)[0].split("@", 1)[0]
    normalized = cleaned.replace("_", "-")
    if not normalized:
        return None
    parts = normalized.split("-")
    if parts[0].casefold() != "zh":
        return cleaned

    script = next((part.casefold() for part in parts[1:]
                   if len(part) == 4 and part.isalpha()), None)
    region = next((part.casefold() for part in reversed(parts[1:])
                   if len(part) == 2 and part.isalpha()), None)
    if script == "hant":
        return None
    if script == "hans":
        return SIMPLIFIED_CHINESE_CATALOG
    if region in _TRADITIONAL_CHINESE_REGIONS:
        return None
    if region in {None, "cn", "sg"}:
        return SIMPLIFIED_CHINESE_CATALOG
    return None


def environment_languages(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Return locale candidates in gettext environment-variable order."""

    values = os.environ if environ is None else environ
    language_list = values.get("LANGUAGE", "")
    if language_list:
        candidates: Sequence[str] = language_list.split(":")
    else:
        candidates = (
            values.get("LC_ALL", ""),
            values.get("LC_MESSAGES", ""),
            values.get("LANG", ""),
        )
    selected = tuple(value.strip() for value in candidates if value.strip())
    if selected:
        return selected
    try:
        system_locale = locale.getlocale()[0]
    except (TypeError, ValueError):
        system_locale = None
    return (system_locale,) if system_locale else ()


def catalog_languages(tags: Sequence[str]) -> tuple[str, ...]:
    """Normalize candidate locale tags and discard unsupported Chinese variants."""

    if isinstance(tags, str):
        tags = (tags,)
    languages: list[str] = []
    chinese_seen = False
    for tag in tags:
        normalized = (
            tag.strip().replace("_", "-").split(".", 1)[0].split("@", 1)[0]
        )
        is_chinese = normalized.split("-", 1)[0].casefold() == "zh"
        if is_chinese:
            if chinese_seen:
                continue
            chinese_seen = True
        selected = catalog_language(tag)
        if selected is not None and selected not in languages:
            languages.append(selected)
    return tuple(languages)


__all__ = [
    "SIMPLIFIED_CHINESE_CATALOG",
    "catalog_language",
    "catalog_languages",
    "environment_languages",
]
