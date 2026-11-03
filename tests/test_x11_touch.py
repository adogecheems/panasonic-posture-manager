"""Tests for X11 touchscreen discovery and coordinate mapping."""

import unittest
from types import SimpleNamespace

from panasonic_posture.protocol import ErrorCode
from panasonic_posture.session.errors import DisplayError
from panasonic_posture.session.x11_touch import (
    CTM_PROPERTY,
    TOUCH_DEVICE_ENV,
    TOUCH_MAPPING_KIND,
    X11TouchMapping,
    parse_ctm,
    select_touch_devices,
)

IDENTITY = (
    "1.000000",
    "0.000000",
    "0.000000",
    "0.000000",
    "1.000000",
    "0.000000",
    "0.000000",
    "0.000000",
    "1.000000",
)


def direct_device(name, phys="usb-0000:00:14.0-9/input0", multitouch=True):
    return {
        "name": name,
        "phys": phys,
        "features": {"direct": True, "multitouch": multitouch},
    }


def properties(name, matrix=IDENTITY):
    return (
        f"Device '{name}':\n"
        f"\t{CTM_PROPERTY} (195):\t" + ", ".join(matrix) + "\n"
    )


class ParseTests(unittest.TestCase):
    def test_coordinate_matrix_is_parsed_from_xinput_output(self):
        self.assertEqual(parse_ctm(properties("touch")), IDENTITY)

    def test_missing_or_invalid_matrix_is_rejected(self):
        self.assertIsNone(parse_ctm("Device 'touch':\n"))
        self.assertIsNone(parse_ctm(properties("touch", ("1", "0"))))
        self.assertIsNone(parse_ctm(properties("touch", ("nan",) + IDENTITY[1:])))


class SelectionTests(unittest.TestCase):
    def test_touchscreen_and_stylus_sharing_a_phys_are_one_panel(self):
        devices = [
            direct_device("Panel Touchscreen"),
            direct_device("Panel Touchscreen Stylus", multitouch=False),
        ]
        self.assertEqual(
            select_touch_devices(devices, ["Panel Touchscreen", "Panel Touchscreen Stylus"]),
            ("Panel Touchscreen", "Panel Touchscreen Stylus"),
        )

    def test_touchscreen_and_stylus_with_different_input_suffixes_are_one_panel(self):
        devices = [
            direct_device("Panel Touchscreen", "usb-panel/input0"),
            direct_device("Panel Stylus", "usb-panel/input1", multitouch=False),
        ]
        self.assertEqual(
            select_touch_devices(devices, ["Panel Touchscreen", "Panel Stylus"]),
            ("Panel Stylus", "Panel Touchscreen"),
        )

    def test_multiple_unrelated_direct_devices_are_ambiguous(self):
        devices = [
            direct_device("Built-in touch", "usb-internal"),
            direct_device("External touch", "usb-external"),
        ]
        self.assertEqual(
            select_touch_devices(devices, ["Built-in touch", "External touch"]),
            (),
        )

    def test_explicit_override_wins_and_can_select_multiple_devices(self):
        devices = [
            direct_device("Built-in touch", "usb-internal"),
            direct_device("External touch", "usb-external"),
        ]
        self.assertEqual(
            select_touch_devices(
                devices,
                ["Built-in touch", "External touch"],
                forced=("External touch",),
            ),
            ("External touch",),
        )

    def test_devices_not_visible_to_xinput_are_ignored(self):
        devices = [direct_device("Kernel name"), direct_device("Another")]
        self.assertEqual(select_touch_devices(devices, ["Kernel name"]), ("Kernel name",))


class FakeXinput:
    def __init__(self):
        self.calls = []
        self.matrices = {
            "Panel Touchscreen": IDENTITY,
            "Panel Touchscreen Stylus": IDENTITY,
        }

    def __call__(self, args, timeout):
        del timeout
        self.calls.append(list(args))
        if args[1:] == ["list", "--name-only"]:
            return SimpleNamespace(
                returncode=0,
                stdout="\n".join(self.matrices) + "\n",
                stderr="",
            )
        if args[1] == "list-props":
            return SimpleNamespace(
                returncode=0,
                stdout=properties(args[2], self.matrices[args[2]]),
                stderr="",
            )
        if args[1] == "map-to-output":
            self.matrices[args[2]] = (
                "0.000000",
                "1.000000",
                "0.000000",
                "-1.000000",
                "0.000000",
                "1.000000",
                "0.000000",
                "0.000000",
                "1.000000",
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[1] == "set-prop":
            self.matrices[args[2]] = tuple(args[4:])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.devices = [
            direct_device("Panel Touchscreen"),
            direct_device("Panel Touchscreen Stylus", multitouch=False),
        ]

    def mapper(self, runner):
        return X11TouchMapping(
            runner,
            which=lambda _name: "/usr/bin/xinput",
            devices=lambda: self.devices,
        )

    def test_capture_map_and_exact_restore(self):
        runner = FakeXinput()
        mapper = self.mapper(runner)
        snapshot = mapper.capture()
        self.assertEqual(snapshot["kind"], TOUCH_MAPPING_KIND)
        self.assertEqual([device["name"] for device in snapshot["devices"]], [
            "Panel Touchscreen",
            "Panel Touchscreen Stylus",
        ])

        mapper.map_to_output(snapshot, "eDP-1")
        self.assertEqual(
            [call for call in runner.calls if call[1] == "map-to-output"],
            [
                ["/usr/bin/xinput", "map-to-output", "Panel Touchscreen", "eDP-1"],
                ["/usr/bin/xinput", "map-to-output", "Panel Touchscreen Stylus", "eDP-1"],
            ],
        )

        mapper.restore(snapshot)
        restored = [call for call in runner.calls if call[1] == "set-prop"]
        self.assertEqual(restored[0][:4], [
            "/usr/bin/xinput",
            "set-prop",
            "Panel Touchscreen",
            CTM_PROPERTY,
        ])
        self.assertEqual(tuple(restored[0][4:]), IDENTITY)

    def test_missing_xinput_disables_mapping_without_running_commands(self):
        calls = []
        mapper = X11TouchMapping(
            lambda args, timeout: calls.append((args, timeout)),
            which=lambda _name: None,
            devices=lambda: self.devices,
        )
        self.assertIsNone(mapper.capture())
        self.assertEqual(calls, [])

    def test_ambiguous_devices_disable_automatic_mapping(self):
        self.devices = [
            direct_device("Built-in touch", "usb-internal"),
            direct_device("External touch", "usb-external"),
        ]
        runner = FakeXinput()
        runner.matrices = {
            "Built-in touch": IDENTITY,
            "External touch": IDENTITY,
        }
        self.assertIsNone(self.mapper(runner).capture())

    def test_explicit_device_environment_resolves_ambiguity(self):
        self.devices = [
            direct_device("Built-in touch", "usb-internal"),
            direct_device("External touch", "usb-external"),
        ]
        runner = FakeXinput()
        runner.matrices = {
            "Built-in touch": IDENTITY,
            "External touch": IDENTITY,
        }
        mapper = X11TouchMapping(
            runner,
            which=lambda _name: "/usr/bin/xinput",
            devices=lambda: self.devices,
            environ={TOUCH_DEVICE_ENV: "Built-in touch"},
        )
        snapshot = mapper.capture()
        self.assertEqual(
            [device["name"] for device in snapshot["devices"]],
            ["Built-in touch"],
        )

    def test_query_failure_leaves_touch_mapping_unchanged(self):
        def failing_runner(args, timeout):
            del timeout
            if args[1:] == ["list", "--name-only"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="xinput failed")
            raise AssertionError(args)

        self.assertIsNone(self.mapper(failing_runner).capture())

    def test_restore_rejects_a_malformed_snapshot(self):
        mapper = self.mapper(FakeXinput())
        with self.assertRaises(DisplayError) as caught:
            mapper.restore({"kind": TOUCH_MAPPING_KIND, "devices": [{"name": "x"}]})
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        with self.assertRaises(DisplayError):
            mapper.restore(
                {
                    "kind": TOUCH_MAPPING_KIND,
                    "devices": [{"name": "x", "matrix": ["not-a-number"] * 9}],
                }
            )


if __name__ == "__main__":
    unittest.main()
