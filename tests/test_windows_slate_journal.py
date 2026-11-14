"""CSM recovery records must survive storage failures without losing ownership."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from panasonic_posture.windows.hardware.slate_journal import SlateJournal


class SlateJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'slate.json'
        self.journal = SlateJournal(self.path)

    def test_read_failure_preserves_the_original_record(self):
        self.journal.save(False, True)
        original = self.path.read_bytes()
        with patch.object(Path, 'read_text', side_effect=PermissionError('fixture denied')):
            with self.assertRaises(OSError):
                self.journal.load()
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_quarantine_is_not_treated_as_an_absent_record(self):
        self.path.write_bytes(b'{')
        with patch.object(Path, 'replace', side_effect=PermissionError('fixture denied')):
            with self.assertRaises(OSError):
                self.journal.load()
        self.assertEqual(self.path.read_bytes(), b'{')

    def test_clear_failure_does_not_report_success(self):
        self.journal.save(False, True)
        with patch.object(Path, 'unlink', side_effect=PermissionError('fixture denied')):
            with self.assertRaises(OSError):
                self.journal.clear()
        self.assertIsNotNone(self.journal.load())

    def test_boolean_version_is_quarantined(self):
        self.path.write_text('{"version":true,"original":false,"desired":true}', encoding='utf-8')
        self.assertIsNone(self.journal.load())
        self.assertFalse(self.path.exists())
        self.assertEqual(len(list(self.path.parent.glob('slate.json.corrupt-*'))), 1)

    def test_nested_corruption_can_be_inspected_read_only_then_isolated(self):
        corrupt = ('[' * 20000 + '0' + ']' * 20000).encode('utf-8')
        self.path.write_bytes(corrupt)
        self.assertIsNone(self.journal.load(quarantine=False))
        self.assertEqual(self.path.read_bytes(), corrupt)
        self.assertIsNone(self.journal.load())
        self.assertFalse(self.path.exists())

    def test_successful_clear_and_missing_records_are_idempotent(self):
        self.assertIsNone(self.journal.load())
        self.journal.save(False, True)
        self.journal.clear()
        self.journal.clear()
        self.assertIsNone(self.journal.load())
