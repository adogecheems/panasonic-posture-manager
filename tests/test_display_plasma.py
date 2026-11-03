"""Tests for the KDE Plasma 6 display backend (no Plasma session required)."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from panasonic_posture.model import Target
from panasonic_posture.protocol import ErrorCode
from panasonic_posture.session.errors import DisplayError
from panasonic_posture.session.journal import Journal
from panasonic_posture.session.plasma import (
    KScreenCtlCommand,
    KScreenDoctorCommand,
    MissingPlasmaCommand,
    PlasmaDisplay,
    build_plasma_command,
    parse_doctor_json,
    parse_kscreenctl_json,
)


def doctor_json(rotation=1, policy=0, output_type=7):
    document = {
        "features": 1,
        "outputs": [
            {
                "id": 1,
                "name": "eDP-1",
                "type": output_type,
                "connected": True,
                "enabled": True,
                "rotation": rotation,
                "autoRotatePolicy": policy,
            },
            {
                "id": 2,
                "name": "HDMI-1",
                "type": 6,
                "connected": True,
                "enabled": True,
                "rotation": 1,
            },
        ],
    }
    return json.dumps(document)


def ctl_json(rotation="none", policy="never"):
    document = [
        {
            "name": "eDP-1",
            "connected": True,
            "enabled": True,
            "rotation": rotation,
            "autoRotate": policy,
            "capabilities": ["auto-rotate", "brightness"],
        },
        {
            "name": "HDMI-1",
            "connected": True,
            "enabled": True,
            "rotation": "none",
            "autoRotate": "never",
            "capabilities": [],
        },
    ]
    return json.dumps(document)


class ParseTests(unittest.TestCase):
    def test_doctor_json_uses_panel_type_and_kscreen_enums(self):
        # KScreen serializes Rotation as RandR bit flags: 4 == inverted.
        output = parse_doctor_json(doctor_json(rotation=4, policy=1))
        self.assertEqual(output.name, "eDP-1")
        self.assertEqual(output.transform, 2)
        self.assertEqual(output.auto_rotate, "in-tablet-mode")
        self.assertTrue(output.auto_rotate_supported)

    def test_doctor_json_without_policy_means_no_lock_support(self):
        document = json.loads(doctor_json())
        document["outputs"][0].pop("autoRotatePolicy")
        output = parse_doctor_json(json.dumps(document))
        self.assertIsNone(output.auto_rotate)
        self.assertFalse(output.auto_rotate_supported)

    def test_kscreenctl_json_maps_string_rotation_and_policy(self):
        output = parse_kscreenctl_json(ctl_json(rotation="180", policy="in-tablet-mode"))
        self.assertEqual(output.name, "eDP-1")
        self.assertEqual(output.transform, 2)
        self.assertEqual(output.auto_rotate, "in-tablet-mode")

    def test_kscreenctl_json_without_capability_ignores_policy(self):
        document = json.loads(ctl_json())
        document[0]["capabilities"] = ["brightness"]
        output = parse_kscreenctl_json(json.dumps(document))
        self.assertIsNone(output.auto_rotate)
        self.assertFalse(output.auto_rotate_supported)

    def test_kscreenctl_empty_capability_list_is_authoritative(self):
        document = json.loads(ctl_json())
        document[0]["capabilities"] = []
        output = parse_kscreenctl_json(json.dumps(document))
        self.assertIsNone(output.auto_rotate)
        self.assertFalse(output.auto_rotate_supported)

    def test_flipped_rotation_is_not_treated_as_plain_rotation(self):
        with self.assertRaises(DisplayError) as caught:
            parse_kscreenctl_json(ctl_json(rotation="flipped180"))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)

    def test_forced_output_name(self):
        self.assertEqual(parse_doctor_json(doctor_json(), "HDMI-1").name, "HDMI-1")

    def test_invalid_json_is_a_stable_error(self):
        with self.assertRaises(DisplayError) as caught:
            parse_doctor_json("not json")
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)


class CommandTests(unittest.TestCase):
    def test_doctor_command_syntax(self):
        calls = []

        def runner(args, timeout):
            del timeout
            calls.append(list(args))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        command = KScreenDoctorCommand("/usr/bin/kscreen-doctor", runner)
        command.set_rotation("eDP-1", 2)
        command.set_policy("eDP-1", "in-tablet-mode")
        self.assertEqual(
            calls,
            [
                ["/usr/bin/kscreen-doctor", "output.eDP-1.rotation.inverted"],
                ["/usr/bin/kscreen-doctor", "output.eDP-1.autoRotatePolicy.inTabletMode"],
            ],
        )

    def test_kscreenctl_command_syntax(self):
        calls = []

        def runner(args, timeout):
            del timeout
            calls.append(list(args))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        command = KScreenCtlCommand("/usr/bin/kscreenctl", runner)
        command.set_rotation("eDP-1", 2)
        command.set_policy("eDP-1", "in-tablet-mode")
        self.assertEqual(
            calls,
            [
                ["/usr/bin/kscreenctl", "eDP-1", "set-rotation", "180"],
                ["/usr/bin/kscreenctl", "eDP-1", "set-auto-rotate", "in-tablet-mode"],
            ],
        )

    def test_factory_prefers_kscreenctl_then_doctor(self):
        ctl = build_plasma_command(
            which=lambda name: "/usr/bin/kscreenctl" if name == "kscreenctl" else None
        )
        doctor = build_plasma_command(
            which=lambda name: None if name == "kscreenctl" else "/usr/bin/kscreen-doctor"
        )
        missing = build_plasma_command(which=lambda _name: None)
        self.assertIsInstance(ctl, KScreenCtlCommand)
        self.assertIsInstance(doctor, KScreenDoctorCommand)
        self.assertIsInstance(missing, MissingPlasmaCommand)


class FakePlasmaCommand:
    name = "fake"
    parse = staticmethod(parse_kscreenctl_json)

    def __init__(self, transform=0, policy="never"):
        self.transform = transform
        self.policy = policy
        self.calls = []

    def query(self):
        names = {0: "none", 1: "90", 2: "180", 3: "270"}
        return ctl_json(names[self.transform], self.policy)

    def set_rotation(self, output, transform):
        self.calls.append(("rotation", output, transform))
        self.transform = transform

    def set_policy(self, output, policy):
        self.calls.append(("policy", output, policy))
        self.policy = policy


class PlasmaTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="posture-plasma-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "display.json"
        self.journal = Journal(self.path)

    def display(self, command, **environ):
        return PlasmaDisplay(command, self.journal, lambda text: text, sleep=lambda _s: None, environ=environ)

    def test_tent_locks_policy_before_rotating(self):
        command = FakePlasmaCommand(policy="in-tablet-mode")
        display = self.display(command)
        display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(command.calls, [("policy", "eDP-1", "never"), ("rotation", "eDP-1", 2)])
        document = self.journal.load()
        self.assertEqual(document["backend"], "plasma")
        self.assertEqual(document["original_policy"], "in-tablet-mode")
        self.assertEqual(document["written_transform"], 2)

    def test_tablet_rotates_before_releasing_the_policy(self):
        command = FakePlasmaCommand(transform=2, policy="never")
        display = self.display(command)
        display.apply(Target(mode="tablet", tablet=None, rotation=None, disable_internal_input=True))
        self.assertEqual(
            command.calls,
            [("rotation", "eDP-1", 0), ("policy", "eDP-1", "in-tablet-mode")],
        )
        self.assertEqual(command.transform, 0)
        self.assertEqual(command.policy, "in-tablet-mode")

    def test_restore_recovers_policy_and_rotation(self):
        command = FakePlasmaCommand(transform=2, policy="always")
        display = self.display(command)
        display.apply(Target(mode="stand", tablet=True, rotation=0, disable_internal_input=True))
        self.assertEqual(command.transform, 0)
        self.assertEqual(command.policy, "never")
        display.apply(Target(mode="hardware"))
        self.assertEqual(command.transform, 2)
        self.assertEqual(command.policy, "in-tablet-mode")
        self.assertIsNotNone(self.journal.load())
        display.apply(Target(mode="unavailable"))
        self.assertIsNone(self.journal.load())

    def test_laptop_keeps_ownership_until_hardware_restores_policy(self):
        command = FakePlasmaCommand(transform=1, policy="in-tablet-mode")
        display = self.display(command)
        display.apply(Target(mode="laptop", tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(command.transform, 0)
        self.assertEqual(command.policy, "never")
        document = self.journal.load()
        self.assertIsNotNone(document)
        self.assertEqual(document["written_transform"], 0)
        display.apply(Target(mode="hardware"))
        self.assertEqual(command.transform, 1)
        self.assertEqual(command.policy, "in-tablet-mode")
        self.assertIsNotNone(self.journal.load())
        display.apply(Target(mode="unavailable"))
        self.assertIsNone(self.journal.load())

    def test_tent_to_laptop_to_hardware_keeps_original_ownership(self):
        command = FakePlasmaCommand(transform=2, policy="always")
        display = self.display(command)
        display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        display.apply(Target(mode="laptop", tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(command.transform, 0)
        self.assertEqual(command.policy, "never")
        display.apply(Target(mode="hardware"))
        self.assertEqual(command.transform, 2)
        self.assertEqual(command.policy, "in-tablet-mode")
        self.assertIsNotNone(self.journal.load())
        display.apply(Target(mode="unavailable"))
        self.assertIsNone(self.journal.load())

    def test_hardware_tablet_unlocks_original_policy(self):
        command = FakePlasmaCommand(transform=2, policy="never")
        display = self.display(command)
        display.apply(Target(mode="hardware"))
        self.assertEqual(command.policy, "in-tablet-mode")
        self.assertEqual(command.transform, 2)
        self.assertIsNotNone(self.journal.load())

        display.apply(Target(mode="unavailable"))
        self.assertEqual(command.policy, "never")
        self.assertIsNone(self.journal.load())

    def test_missing_binary_reports_display_unavailable(self):
        display = self.display(MissingPlasmaCommand("not-found"))
        with self.assertRaises(DisplayError) as caught:
            display.apply(Target(mode="tent"))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
