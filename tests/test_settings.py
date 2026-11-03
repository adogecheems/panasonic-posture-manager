"""Tests for :mod:`panasonic_posture.service.settings`."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from panasonic_posture.service import settings as settings_module
from panasonic_posture.service.settings import Settings, SettingsStore

CORRUPT_GLOB = "settings.json.corrupt-*"


class SettingsLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "settings.json"

    def store(self) -> SettingsStore:
        return SettingsStore(self.path)

    def test_missing_file_returns_defaults(self) -> None:
        self.assertEqual(self.store().load(), Settings(tent_prompt=True))
        self.assertFalse(self.path.exists())

    def test_round_trip_false(self) -> None:
        self.store().save(Settings(tent_prompt=False))
        self.assertEqual(self.store().load(), Settings(tent_prompt=False))

    def test_round_trip_true(self) -> None:
        self.store().save(Settings(tent_prompt=True))
        self.assertEqual(self.store().load(), Settings(tent_prompt=True))

    def test_round_trip_stand_prompt(self) -> None:
        self.store().save(Settings(tent_prompt=True, stand_prompt=True))
        self.assertEqual(
            self.store().load(), Settings(tent_prompt=True, stand_prompt=True)
        )

    def test_round_trip_sensor_calibration(self) -> None:
        matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
        self.store().save(Settings(sensor_calibration=matrix))
        self.assertEqual(
            self.store().load(), Settings(sensor_calibration=matrix)
        )

    def test_payload_has_version_and_boolean(self) -> None:
        self.store().save(Settings(tent_prompt=False))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(
            data,
            {
                "version": 1,
                "tent_prompt": False,
                "stand_prompt": False,
                "sensor_calibration": None,
            },
        )

    def test_legacy_payload_without_stand_prompt_uses_safe_default(self) -> None:
        self.path.write_text(
            '{"version": 1, "tent_prompt": true}', encoding="utf-8"
        )
        self.assertEqual(self.store().load(), Settings(tent_prompt=True))
        self.assertEqual(list(self.path.parent.glob(CORRUPT_GLOB)), [])

    def test_save_is_atomic_and_private(self) -> None:
        store = self.store()
        store.save(Settings(tent_prompt=False))
        self.assertTrue(self.path.is_file())
        mode = stat.S_IMODE(self.path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        self.assertEqual(list(self.path.parent.glob(".settings.json.tmp-*")), [])
        self.assertEqual(list(self.path.parent.glob("settings.json.corrupt-*")), [])

    def test_save_creates_parent_directory_with_private_mode(self) -> None:
        path = Path(self._directory.name) / "nested" / "settings.json"
        SettingsStore(path).save(Settings())
        self.assertTrue(path.is_file())
        mode = stat.S_IMODE(path.parent.stat().st_mode)
        self.assertEqual(mode, 0o700)

    def test_save_replaces_existing_file(self) -> None:
        self.path.write_text("invalid", encoding="utf-8")
        self.store().save(Settings(tent_prompt=False))
        self.assertEqual(self.store().load(), Settings(tent_prompt=False))
        self.assertEqual(list(self.path.parent.glob(CORRUPT_GLOB)), [])

    def test_save_fsyncs_file_and_directory(self) -> None:
        store = self.store()
        with mock.patch.object(settings_module.os, "fsync") as fsync:
            store.save(Settings())
        self.assertGreaterEqual(fsync.call_count, 2)

    def test_save_failure_keeps_previous_file_and_cleans_temp(self) -> None:
        store = self.store()
        store.save(Settings(tent_prompt=True))
        with (
            mock.patch.object(
                settings_module.os, "replace", side_effect=OSError("disk full")
            ),
            self.assertRaises(OSError),
        ):
            store.save(Settings(tent_prompt=False))
        self.assertEqual(store.load(), Settings(tent_prompt=True))
        self.assertEqual(list(self.path.parent.glob(".settings.json.tmp-*")), [])

    def test_save_rejects_foreign_object(self) -> None:
        with self.assertRaises(TypeError):
            self.store().save({"tent_prompt": True})  # type: ignore[arg-type]

    def test_unreadable_path_returns_defaults_without_quarantine(self) -> None:
        self.path.mkdir()
        self.assertEqual(self.store().load(), Settings())
        self.assertEqual(list(self.path.parent.glob(CORRUPT_GLOB)), [])


class SettingsCorruptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "settings.json"

    def corrupt_files(self) -> list[Path]:
        return sorted(self.path.parent.glob(CORRUPT_GLOB))

    def test_non_utf8_file_is_quarantined(self) -> None:
        payload = b'\xff\xfe{"version": 1, "tent_prompt": true}'
        self.path.write_bytes(payload)
        store = SettingsStore(self.path)
        self.assertEqual(store.load(), Settings())
        self.assertFalse(self.path.exists())
        quarantined = self.corrupt_files()
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), payload)
        # The corrupt file is gone, so the next load is quiet and uses defaults.
        self.assertEqual(store.load(), Settings())
        self.assertEqual(len(self.corrupt_files()), 1)

    def test_invalid_json_is_quarantined(self) -> None:
        text = "{this is not json"
        self.path.write_text(text, encoding="utf-8")
        store = SettingsStore(self.path)
        self.assertEqual(store.load(), Settings())
        self.assertFalse(self.path.exists())
        quarantined = self.corrupt_files()
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), text)
        # A second load uses defaults and does not quarantine again.
        self.assertEqual(store.load(), Settings())
        self.assertEqual(len(self.corrupt_files()), 1)

    def test_invalid_schemas_are_quarantined(self) -> None:
        cases = [
            "[]",
            '"text"',
            "5",
            "{}",
            '{"version": 2, "tent_prompt": true}',
            '{"version": 1, "tent_prompt": "yes"}',
            '{"version": 1, "tent_prompt": 1}',
            '{"version": 1, "tent_prompt": true, "stand_prompt": "yes"}',
            '{"version": 1, "tent_prompt": true, "stand_prompt": 1}',
            '{"version": 1, "tent_prompt": true, "sensor_calibration": []}',
            '{"version": 1, "tent_prompt": true, "sensor_calibration": [1, 0, 0]}',
            '{"version": 1, "tent_prompt": true, "sensor_calibration": [2, 0, 0, 0, 2, 0, 0, 0, 2]}',
            '{"tent_prompt": true}',
        ]
        for index, text in enumerate(cases):
            with self.subTest(text=text):
                for old in self.corrupt_files():
                    old.unlink()
                self.path.write_text(text, encoding="utf-8")
                self.assertEqual(SettingsStore(self.path).load(), Settings())
                quarantined = self.corrupt_files()
                self.assertEqual(len(quarantined), 1)
                self.assertEqual(quarantined[0].read_text(encoding="utf-8"), text)

    def test_quarantine_name_uses_timestamp_and_avoids_collisions(self) -> None:
        clock = lambda: 1_700_000_000.0
        self.path.write_text("{broken", encoding="utf-8")
        SettingsStore(self.path, time_source=clock).load()
        self.path.write_text("{broken too", encoding="utf-8")
        SettingsStore(self.path, time_source=clock).load()
        names = [path.name for path in self.corrupt_files()]
        self.assertEqual(len(names), 2)
        self.assertRegex(names[0], r"^settings\.json\.corrupt-\d{8}T\d{6}$")
        self.assertRegex(names[1], r"^settings\.json\.corrupt-\d{8}T\d{6}-1$")

    def test_save_after_corruption_writes_valid_file(self) -> None:
        self.path.write_text("{broken", encoding="utf-8")
        store = SettingsStore(self.path)
        self.assertEqual(store.load(), Settings())
        store.save(Settings(tent_prompt=False))
        self.assertEqual(store.load(), Settings(tent_prompt=False))
        self.assertEqual(len(self.corrupt_files()), 1)


if __name__ == "__main__":
    unittest.main()
