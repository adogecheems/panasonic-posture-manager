"""Native text rendering shared by the lightweight shell and Fluent pages."""
from PyQt6.QtGui import QFont, QFontDatabase
from PyQt6.QtWidgets import QApplication


def configure_application_font():
    """Keep system typography with smooth scalable CJK fallback glyphs."""
    app = QApplication.instance()
    if app is None:
        return
    font = QFont(app.font())
    families = [family for family in font.families() if family]
    installed = set(QFontDatabase.families())
    # A Latin system font needs an explicit CJK UI fallback. Leaving this to
    # platform font linking can select legacy glyphs at small text sizes.
    for family in ('Microsoft YaHei UI', 'Microsoft YaHei'):
        if family in installed:
            if family not in families:
                families.append(family)
            break
    if families:
        font.setFamilies(families)
    # Full grid fitting steps horizontal outlines at fractional DPI. Keep
    # vertical alignment and allow the native renderer to use ClearType.
    font.setHintingPreference(QFont.HintingPreference.PreferVerticalHinting)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias |
                          QFont.StyleStrategy.PreferOutline)
    app.setFont(font)
