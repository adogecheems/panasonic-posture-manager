"""Bounded backend diagnostics without touching a real user profile."""
import logging
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.windows.common.diagnostics import create_handler, log_path


def emit(handler: logging.Handler, message: str) -> None:
    handler.emit(logging.LogRecord(
        'fixture', logging.WARNING, __file__, 1, message, (), None,
    ))


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_log_is_adjacent_to_the_exact_settings_file(self):
        root = Path(self.directory.name)
        self.assertEqual(log_path(root / 'settings.json'), root / 'settings.log')
        self.assertEqual(log_path(root / 'custom'), root / 'custom.log')

    def test_private_backend_log_rotates_with_one_bounded_backup(self):
        settings = Path(self.directory.name) / 'settings.json'
        path = log_path(settings)
        path.write_text('old-entry\n', encoding='utf-8')
        handler = create_handler(settings, limit=16)
        try:
            emit(handler, 'new-entry')
        finally:
            handler.close()
        self.assertIn('new-entry', path.read_text(encoding='utf-8'))
        backup = path.with_name(path.name + '.1')
        self.assertEqual(backup.read_text(encoding='utf-8'), 'old-entry\n')
        self.assertLessEqual(path.stat().st_size, 16)
        self.assertLessEqual(backup.stat().st_size, 16)


if __name__ == '__main__':
    unittest.main()
