"""Per-user appearance storage, independent of privileged hardware settings."""
from pathlib import Path

from PyQt6.QtCore import QSettings
from qfluentwidgets import Theme


class AppearanceStore:
    def __init__(self, path: Path | None) -> None:
        self.path = path

    def _settings(self) -> QSettings:
        if self.path is None:
            raise OSError('appearance settings directory is unavailable')
        settings = QSettings(str(self.path), QSettings.Format.IniFormat)
        settings.setAtomicSyncRequired(True)
        settings.sync()
        self._check(settings)
        return settings

    @staticmethod
    def _check(settings: QSettings) -> None:
        if settings.status() != QSettings.Status.NoError:
            raise OSError('appearance settings are unavailable')

    def load(self) -> Theme:
        value = self._settings().value('appearance', Theme.AUTO.value)
        # Refuse invalid data on save too, preserving it for diagnosis.
        return Theme(value)

    def save(self, theme: Theme) -> None:
        if not isinstance(theme, Theme):
            raise ValueError('invalid appearance')
        self.load()
        settings = self._settings()
        settings.setValue('appearance', theme.value)
        settings.sync()
        self._check(settings)
