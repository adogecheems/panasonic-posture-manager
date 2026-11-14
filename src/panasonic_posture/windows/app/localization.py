"""Windows language-ID mapping for the shared localization catalog."""

from panasonic_posture.localization import SIMPLIFIED_CHINESE_CATALOG

_SIMPLIFIED_CHINESE_LANGUAGE_IDS = frozenset({0x0004, 0x0804, 0x1004})


def windows_catalog_language(language_id: int) -> str:
    """Map a Windows LANGID to the project language used by the UI."""

    return (
        SIMPLIFIED_CHINESE_CATALOG
        if language_id in _SIMPLIFIED_CHINESE_LANGUAGE_IDS
        else "en"
    )


__all__ = ["windows_catalog_language"]
