"""Tests for panasonic_posture.hardware.profile (strict validation + lookup)."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from panasonic_posture.hardware import profile as hardware_profile
from panasonic_posture.hardware.profile import (
    PROFILE_DIRS,
    Profile,
    apply_mount,
    dmi,
    load_profile,
    parse_mount_matrix,
)

PROFILES = Path(__file__).resolve().parents[1] / "data" / "profiles"
LOGGER = "panasonic_posture.hardware.profile"


def write_json(directory, name, data, *, raw=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(json.dumps(data), encoding="utf-8")
    return path


def matching_profile(identifier="custom", **extra):
    data = {
        "id": identifier,
        "match": {"vendor": "Panasonic Corporation", "product": "CUSTOM-1"},
    }
    data.update(extra)
    return data


class BuiltinProfileTests(unittest.TestCase):
    def test_cfqv8_profile_is_found(self):
        profile = load_profile(("Panasonic Corporation", "CFQV8-1L"), [PROFILES])
        self.assertIsInstance(profile, Profile)
        self.assertEqual(profile.id, "cfqv8")
        self.assertEqual(profile.vendor, "Panasonic Corporation")
        self.assertEqual(profile.product, "CFQV8-1L")
        self.assertEqual(profile.switch_name, "Intel HID switches")
        self.assertEqual(profile.keyboard_name, "AT Translated Set 2 keyboard")
        self.assertEqual(profile.accel_name, "accel_3d")
        self.assertEqual(profile.support_tier, "validated")
        self.assertTrue(profile.supported)
        self.assertEqual(profile.label, "CFQV8-1L")

    def test_qv9_and_qv1_profiles_are_found(self):
        for product, identifier in (
            ("CFQV9-1L", "cfqv9"),
            ("CF-QV9-2L", "cfqv9"),
            ("CFQV1-1L", "cfqv1"),
            ("CF-QV1-2L", "cfqv1"),
        ):
            with self.subTest(product=product):
                profile = load_profile(("Panasonic Corporation", product), [PROFILES])
                self.assertEqual(profile.id, identifier)
                self.assertEqual(profile.support_tier, "validated")
                self.assertEqual(profile.switch_name, "Intel HID switches")

    def test_unknown_panasonic_uses_supported_compatible_profile(self):
        profile = load_profile(("Panasonic Corporation", "CF-UNKNOWN"), [PROFILES])
        self.assertEqual(profile.id, "generic")
        self.assertEqual(profile.support_tier, "compatible")
        self.assertTrue(profile.supported)
        self.assertIsNone(profile.switch_name)
        self.assertIsNone(profile.mount_matrix)

    def test_non_panasonic_uses_supported_compatible_profile(self):
        profile = load_profile(("Other Vendor", "X"), [PROFILES])
        self.assertEqual(profile.id, "generic")
        self.assertEqual(profile.support_tier, "compatible")
        self.assertTrue(profile.supported)

    def test_source_profile_dirs_include_development_tree(self):
        self.assertEqual(PROFILE_DIRS[0], Path("/etc/panasonic-posture-manager/profiles"))
        self.assertEqual(
            PROFILE_DIRS[1], PROFILES
        )
        self.assertEqual(
            PROFILE_DIRS[2], Path("/usr/share/panasonic-posture-manager/profiles")
        )

    def test_development_tree_wins_over_stale_installed_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = Path(tmp) / "installed"
            source = Path(tmp) / "source"
            write_json(
                installed,
                "cfqv8.json",
                {"id": "stale", "match": {"product": "CFQV8-1L"}},
            )
            write_json(
                source,
                "cfqv8.json",
                dict(
                    matching_profile("cfqv8"),
                    match={"product": "CFQV8-1L"},
                    support_tier="validated",
                ),
            )
            profile = load_profile(
                ("Panasonic Corporation", "CFQV8-1L"), [source, installed]
            )
        self.assertEqual(profile.id, "cfqv8")
        self.assertEqual(profile.support_tier, "validated")

    def test_dmi_reader_never_raises(self):
        vendor, product = dmi()
        self.assertIsInstance(vendor, str)
        self.assertIsInstance(product, str)


class LookupOrderTests(unittest.TestCase):
    def test_user_directory_wins_over_installed_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            high = Path(tmp) / "etc"
            low = Path(tmp) / "share"
            write_json(high, "same.json", matching_profile("from-etc"))
            write_json(low, "same.json", matching_profile("from-share"))
            profile = load_profile(
                ("Panasonic Corporation", "CUSTOM-1"), [high, low]
            )
        self.assertEqual(profile.id, "from-etc")

    def test_profiles_are_sorted_within_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp), "b.json", matching_profile("second"))
            write_json(Path(tmp), "a.json", matching_profile("first"))
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "first")

    def test_broken_profile_does_not_mask_later_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp), "a-broken.json", None, raw=b"{not json")
            write_json(Path(tmp), "b-valid.json", matching_profile("valid"))
            with self.assertLogs(LOGGER, level="WARNING"):
                profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "valid")

    def test_missing_directory_is_ignored(self):
        profile = load_profile(
            ("Panasonic Corporation", "CUSTOM-1"),
            [Path("/nonexistent/profile-dir-for-tests")],
        )
        self.assertEqual(profile.id, "generic")

    def test_single_directory_argument_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp), "a.json", matching_profile("single"))
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), tmp)
        self.assertEqual(profile.id, "single")


class MatchSemanticsTests(unittest.TestCase):
    def test_vendor_only_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "vendor.json",
                {"id": "vendor", "match": {"vendor": "Panasonic Corporation"}},
            )
            profile = load_profile(("Panasonic Corporation", "ANY"), [tmp])
        self.assertEqual(profile.id, "vendor")

    def test_product_only_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "product.json",
                {"id": "product", "match": {"product": "CUSTOM-1"}},
            )
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "product")

    def test_string_match_uses_the_product_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp), "short.json", {"id": "short", "match": "CUSTOM-1"}
            )
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "short")

    def test_product_glob_matches_variants_case_insensitively(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "family.json",
                {
                    "id": "family",
                    "match": {
                        "product_glob": ["CFQV8", "CFQV8-*", "CF-QV8-*"],
                    },
                },
            )
            for product in ("CFQV8", "cfqv8-1l", "CF-QV8-2L"):
                with self.subTest(product=product):
                    profile = load_profile(("Any Vendor", product), [tmp])
                    self.assertEqual(profile.id, "family")

    def test_product_glob_does_not_match_other_products(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "family.json",
                {"id": "family", "match": {"product_glob": "CFQV9-*"}},
            )
            profile = load_profile(("Any Vendor", "CFQV8-1L"), [tmp])
        self.assertEqual(profile.id, "generic")

    def test_empty_match_matches_no_machine_and_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp), "empty.json", {"id": "empty", "match": {}})
            with self.assertLogs(LOGGER, level="WARNING") as caught:
                profile = load_profile(("Panasonic Corporation", "CFQV8-1L"), [tmp])
        self.assertEqual(profile.id, "generic")
        self.assertIn("empty match", "\n".join(caught.output))

    def test_missing_or_null_match_matches_nothing(self):
        for data in ({"id": "none"}, {"id": "null", "match": None},
                     {"id": "blank", "match": "   "}):
            with tempfile.TemporaryDirectory() as tmp:
                write_json(Path(tmp), "profile.json", data)
                with self.assertLogs(LOGGER, level="WARNING"):
                    profile = load_profile(("Panasonic Corporation", "X"), [tmp])
            self.assertEqual(profile.id, "generic", data)

    def test_non_matching_vendor_or_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "a.json",
                {"id": "nope", "match": {"vendor": "Other", "product": "CUSTOM-1"}},
            )
            write_json(
                Path(tmp),
                "b.json",
                {"id": "nope2", "match": {"product": "OTHER"}},
            )
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "generic")


class StrictValidationTests(unittest.TestCase):
    def load_single(self, data, *, raw=None):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp), "profile.json", data, raw=raw)
            with self.assertLogs(LOGGER, level="WARNING"):
                profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "generic")
        return profile

    def test_top_level_must_be_an_object(self):
        for data in ([1, 2, 3], "text", 5, True):
            self.load_single(data)

    def test_invalid_match_type_is_rejected(self):
        self.load_single({"id": "bad", "match": [1, 2]})
        self.load_single({"id": "bad", "match": 5})
        self.load_single({"id": "bad", "match": {"vendor": 5}})
        self.load_single({"id": "bad", "match": {"product": []}})
        self.load_single({"id": "bad", "match": {"product_glob": 5}})
        self.load_single({"id": "bad", "match": {"product_glob": [5]}})
        self.load_single({"id": "bad", "match": {"product_glob": ["  "]}})
        self.load_single({"id": "bad", "match": {"product_glob": ["bad\nname"]}})

    def test_invalid_id_is_rejected(self):
        self.load_single({"id": 5, "match": "CUSTOM-1"})
        self.load_single({"id": "  ", "match": "CUSTOM-1"})

    def test_unsupported_version_is_rejected(self):
        self.load_single(dict(matching_profile("v2"), version=2))

    def test_invalid_device_name_types_are_rejected(self):
        for key in ("switch_name", "keyboard_name", "touchpad_name", "accel_name"):
            self.load_single(dict(matching_profile("b"), **{key: 5}))

    def test_control_characters_in_device_names_are_rejected(self):
        for key in ("switch_name", "keyboard_name", "touchpad_name", "accel_name"):
            for bad in ("bad\x00name", "bad\nname", "bad\x7fname"):
                self.load_single(dict(matching_profile("ctl"), **{key: bad}))

    def test_boolean_or_float_version_is_rejected(self):
        self.load_single(dict(matching_profile("v"), version=True))
        self.load_single(dict(matching_profile("v"), version=1.0))

    def test_invalid_utf8_profile_still_loads_or_is_skipped(self):
        raw = (
            b'{"id": "bad-name", "match": {"product": "CUSTOM-1"}, '
            b'"switch_name": "bad'
            + bytes([0xFF])
            + b'name"}'
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "profile.json"
            path.write_bytes(raw)
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "bad-name")
        self.assertIn("\ufffd", profile.switch_name)

    def test_valid_profile_keeps_optional_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "profile.json",
                matching_profile(
                    "full",
                    switch_name="sw",
                    keyboard_name="kb",
                    touchpad_name="tp",
                    accel_name="accel_3d",
                    mount_matrix=[0, -1, 0, 1, 0, 0, 0, 0, 1],
                    support_tier="validated",
                ),
            )
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.switch_name, "sw")
        self.assertEqual(profile.keyboard_name, "kb")
        self.assertEqual(profile.touchpad_name, "tp")
        self.assertEqual(profile.mount_matrix, (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0))
        self.assertEqual(profile.support_tier, "validated")

    def test_invalid_support_tier_is_rejected(self):
        for value in ("unknown", "", 1, True):
            with self.subTest(value=value):
                self.load_single(dict(matching_profile("tier"), support_tier=value))

    def test_unsupported_tier_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "disabled.json",
                dict(matching_profile("disabled"), support_tier="unsupported"),
            )
            profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.support_tier, "unsupported")
        self.assertFalse(profile.supported)


class MountMatrixTests(unittest.TestCase):
    def test_none_is_allowed(self):
        self.assertIsNone(parse_mount_matrix(None))

    def test_identity_matrix_is_allowed(self):
        matrix = parse_mount_matrix([1, 0, 0, 0, 1, 0, 0, 0, 1])
        self.assertEqual(matrix, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))

    def test_rotation_is_allowed(self):
        matrix = parse_mount_matrix([0, -1, 0, 1, 0, 0, 0, 0, 1])
        self.assertIsNotNone(matrix)

    def test_wrong_length_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_mount_matrix([1, 0, 0])
        with self.assertRaises(ValueError):
            parse_mount_matrix([1, 0, 0, 0, 1, 0, 0, 0, 1, 0])

    def test_non_finite_values_are_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            matrix = [1, 0, 0, 0, 1, 0, 0, 0, 1]
            matrix[0] = value
            with self.assertRaises(ValueError):
                parse_mount_matrix(matrix)

    def test_non_orthogonal_matrix_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_mount_matrix([1, 0, 0, 0, 1, 0, 0, 0, 2])
        with self.assertRaises(ValueError):
            parse_mount_matrix([1, 1, 0, 0, 1, 0, 0, 0, 1])

    def test_mount_matrix_with_bool_is_rejected(self):
        matrix = [True, 0, 0, 0, 1, 0, 0, 0, 1]
        with self.assertRaises(ValueError):
            parse_mount_matrix(matrix)

    def test_non_orthogonal_profile_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(
                Path(tmp),
                "bad.json",
                dict(matching_profile("bad"), mount_matrix=[1, 0, 0, 0, 1, 0, 0, 0, 9]),
            )
            write_json(Path(tmp), "good.json", matching_profile("good"))
            with self.assertLogs(LOGGER, level="WARNING"):
                profile = load_profile(("Panasonic Corporation", "CUSTOM-1"), [tmp])
        self.assertEqual(profile.id, "good")

    def test_apply_mount_identity_and_rotation(self):
        self.assertEqual(
            apply_mount((1.0, 2.0, 3.0), None), (1.0, 2.0, 3.0)
        )
        self.assertEqual(
            apply_mount((1.0, 0.0, 0.0), [0, -1, 0, 1, 0, 0, 0, 0, 1]),
            (0.0, 1.0, 0.0),
        )

    def test_apply_mount_validates_shapes(self):
        with self.assertRaises(ValueError):
            apply_mount((1.0, 2.0), None)
        with self.assertRaises(ValueError):
            apply_mount((1.0, 2.0, 3.0), [1, 0, 0])


class LoadProfileArgumentTests(unittest.TestCase):
    def test_bad_dmi_argument_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_profile("not-a-pair")
        with self.assertRaises(ValueError):
            load_profile(("a", "b", "c"))

    def test_dmi_values_alias(self):
        self.assertEqual(hardware_profile.dmi_values(), dmi())

    def test_profile_defaults_to_compatible_tier(self):
        self.assertEqual(Profile().support_tier, "compatible")
        self.assertTrue(Profile().supported)


class DmiFailureTests(unittest.TestCase):
    def test_unreadable_dmi_returns_empty_and_generic_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent-dmi"
            with mock.patch.object(hardware_profile, "DMI_ROOT", missing):
                self.assertEqual(dmi(), ("", ""))
                profile = load_profile(dirs=[Path(tmp) / "profiles"])
        self.assertEqual(profile.id, "generic")
        self.assertEqual(profile.vendor, "")
        self.assertEqual(profile.product, "")
        self.assertEqual(profile.support_tier, "compatible")
        self.assertTrue(profile.supported)


if __name__ == "__main__":
    unittest.main()
