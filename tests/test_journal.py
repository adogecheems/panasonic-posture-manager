"""Tests for the display ownership journal (tempfile based, stdlib only)."""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from panasonic_posture.session.journal import JOURNAL_VERSION, Journal


def document(**overrides):
    data = {
        "version": JOURNAL_VERSION,
        "spec": ["eDP-1", "AUO", "panel", "1"],
        "original_transform": 0,
        "original_lock": False,
        "written_lock": True,
        "written_transform": 2,
    }
    data.update(overrides)
    return data


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="posture-journal-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "state"
        self.path = self.directory / "display.json"
        self.journal = Journal(self.path, clock=lambda: 1234)

    # -- happy path -------------------------------------------------------

    def test_missing_journal_is_none(self):
        self.assertIsNone(self.journal.load())

    def test_round_trip_is_atomic_and_private(self):
        data = document()
        self.journal.save(data)
        self.assertEqual(self.journal.load(), data)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        leftovers = [entry.name for entry in self.directory.iterdir() if entry.name != "display.json"]
        self.assertEqual(leftovers, [])
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), data)

    def test_save_overwrites_previous_document(self):
        self.journal.save(document())
        updated = document(original_transform=1)
        self.journal.save(updated)
        self.assertEqual(self.journal.load(), updated)

    def test_x11_document_round_trip(self):
        data = document(backend="x11", spec=["eDP-1"], written_lock=None)
        self.journal.save(data)
        self.assertEqual(self.journal.load(), data)

    def test_x11_touch_auxiliary_round_trip(self):
        auxiliary = {
            "kind": "x11-touch-mapping",
            "devices": [
                {
                    "name": "Panel Touchscreen",
                    "matrix": ["1.000000"] * 9,
                }
            ],
        }
        data = document(
            backend="x11",
            spec=["eDP-1"],
            written_lock=None,
            original_auxiliary=auxiliary,
            written_auxiliary=True,
        )
        self.journal.save(data)
        self.assertEqual(self.journal.load(), data)

    def test_plasma_document_round_trip_with_policy(self):
        data = document(
            backend="plasma",
            spec=["eDP-1"],
            original_policy="in-tablet-mode",
        )
        self.journal.save(data)
        self.assertEqual(self.journal.load(), data)

    def test_clear_removes_the_file(self):
        self.journal.save(document())
        self.journal.clear()
        self.assertIsNone(self.journal.load())
        self.assertFalse(self.path.exists())
        self.journal.clear()  # idempotent

    # -- corruption isolation --------------------------------------------

    def test_invalid_json_is_quarantined(self):
        self.directory.mkdir(parents=True)
        self.path.write_text("not json at all", encoding="utf-8")
        self.assertIsNone(self.journal.load())
        self.assertFalse(self.path.exists())
        quarantined = self.directory / "display.json.corrupt-1234"
        self.assertTrue(quarantined.is_file())
        self.assertEqual(quarantined.read_text(encoding="utf-8"), "not json at all")

    def test_non_object_json_is_quarantined(self):
        self.directory.mkdir(parents=True)
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        self.assertIsNone(self.journal.load())
        self.assertTrue((self.directory / "display.json.corrupt-1234").is_file())

    def test_wrong_version_is_quarantined(self):
        self.directory.mkdir(parents=True)
        self.path.write_text(json.dumps(document(version=99)), encoding="utf-8")
        self.assertIsNone(self.journal.load())
        self.assertTrue((self.directory / "display.json.corrupt-1234").is_file())

    def test_invalid_field_types_are_quarantined(self):
        for override in (
            {"spec": ["eDP-1"]},
            {"spec": ["eDP-1", "AUO", "panel", 1]},
            {"original_transform": "0"},
            {"original_lock": 0},
            {"written_lock": "true"},
            {"written_transform": 2.5},
            {"original_policy": 0},
            {"backend": "x11", "spec": ["eDP-1", "extra"]},
            {"original_auxiliary": []},
            {"written_auxiliary": "true"},
        ):
            with self.subTest(override=override):
                self.directory.mkdir(parents=True, exist_ok=True)
                for entry in self.directory.iterdir():
                    entry.unlink()
                self.path.write_text(json.dumps(document(**override)), encoding="utf-8")
                self.assertIsNone(self.journal.load())
                self.assertTrue((self.directory / "display.json.corrupt-1234").is_file())

    def test_corrupt_files_do_not_overwrite_each_other(self):
        self.directory.mkdir(parents=True)
        fixed_clock = lambda: 7
        journal = Journal(self.path, clock=fixed_clock)
        self.path.write_text("{", encoding="utf-8")
        self.assertIsNone(journal.load())
        self.path.write_text("{", encoding="utf-8")
        self.assertIsNone(journal.load())
        names = sorted(entry.name for entry in self.directory.iterdir())
        self.assertEqual(
            names,
            ["display.json.corrupt-7", "display.json.corrupt-7-1"],
        )

    # -- invalid writes ---------------------------------------------------

    def test_save_rejects_invalid_documents(self):
        for invalid in (None, [], {}, document(version=2), document(original_lock="yes")):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    self.journal.save(invalid)  # type: ignore[arg-type]
                self.assertFalse(self.path.exists())

    def test_read_error_does_not_quarantine(self):
        self.directory.mkdir(parents=True)
        self.path.write_text(json.dumps(document()), encoding="utf-8")
        os.chmod(self.path, 0o000)
        try:
            self.assertIsNone(self.journal.load())
        finally:
            os.chmod(self.path, 0o600)
        self.assertTrue(self.path.exists())


if __name__ == "__main__":
    unittest.main()
