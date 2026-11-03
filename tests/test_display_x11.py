"""Tests for the X11/RandR display backend (no X server required)."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from panasonic_posture.model import Target
from panasonic_posture.protocol import ErrorCode
from panasonic_posture.session.errors import DisplayError
from panasonic_posture.session.journal import Journal
from panasonic_posture.session.x11 import (
    ROTATION_TO_TRANSFORM,
    X11Display,
    parse_xrandr_query,
)
from panasonic_posture.session.x11_touch import TOUCH_MAPPING_KIND


def randr_query(rotation="normal", external=False, internal=True):
    names = {"normal": "normal", "left": "left", "inverted": "inverted", "right": "right"}
    lines = ["Screen 0: minimum 8 x 8, current 2880 x 1920, maximum 32767 x 32767"]
    if internal:
        lines.append(
            "eDP-1 connected primary 2880x1920+0+0 "
            f"({names[rotation]} left inverted right x axis y axis) 344mm x 215mm"
        )
    if external:
        lines.append(
            "HDMI-1 connected 1920x1080+2880+0 "
            "(normal left inverted right x axis y axis) 527mm x 296mm"
        )
    lines.append("DP-9 disconnected (normal left inverted right x axis y axis)")
    return "\n".join(lines) + "\n"


class FakeXrandr:
    def __init__(self, rotation="normal", external=False, fail_rotate=0, ignore_rotate=False):
        self.rotation = rotation
        self.external = external
        self.fail_rotate = fail_rotate
        self.ignore_rotate = ignore_rotate
        self.calls = []

    def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args == ["xrandr", "--query"]:
            return SimpleNamespace(
                returncode=0,
                stdout=randr_query(self.rotation, external=self.external),
                stderr="",
            )
        if args[:2] == ["xrandr", "--output"]:
            if self.fail_rotate:
                self.fail_rotate -= 1
                return SimpleNamespace(returncode=1, stdout="", stderr="rotation failed")
            if not self.ignore_rotate:
                self.rotation = args[args.index("--rotate") + 1]
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)


class FakeTouch:
    def __init__(self, fail_map=False, fail_restore=False):
        self.fail_map = fail_map
        self.fail_restore = fail_restore
        self.capture_enabled = True
        self.mapped = []
        self.restored = []
        self.snapshot = {
            "kind": TOUCH_MAPPING_KIND,
            "devices": [
                {
                    "name": "eGalax Touchscreen",
                    "matrix": [
                        "1.000000",
                        "0.000000",
                        "0.000000",
                        "0.000000",
                        "1.000000",
                        "0.000000",
                        "0.000000",
                        "0.000000",
                        "1.000000",
                    ],
                }
            ],
        }

    def capture(self):
        return self.snapshot if self.capture_enabled else None

    def map_to_output(self, snapshot, output):
        if self.fail_map:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "touch-map-failed")
        self.mapped.append((snapshot, output))

    def restore(self, snapshot):
        if self.fail_restore:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "touch-restore-failed")
        self.restored.append(snapshot)


class ParseTests(unittest.TestCase):
    def test_internal_panel_is_selected_over_external(self):
        output = parse_xrandr_query(randr_query(external=True))
        self.assertEqual(output.name, "eDP-1")
        self.assertEqual(output.transform, 0)

    def test_orientation_is_mapped(self):
        for name, transform in ROTATION_TO_TRANSFORM.items():
            with self.subTest(name=name):
                self.assertEqual(parse_xrandr_query(randr_query(name)).transform, transform)

    def test_single_connected_output_is_used_as_fallback(self):
        text = (
            "Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767\n"
            "DP-1 connected 1920x1080+0+0 (normal left inverted right) 527mm x 296mm\n"
        )
        self.assertEqual(parse_xrandr_query(text).name, "DP-1")

    def test_multiple_unknown_outputs_are_rejected(self):
        text = (
            "Screen 0: minimum 8 x 8, current 3840 x 1080, maximum 32767 x 32767\n"
            "DP-1 connected 1920x1080+0+0 (normal left inverted right) 527mm x 296mm\n"
            "DP-2 connected 1920x1080+1920+0 (normal left inverted right) 527mm x 296mm\n"
        )
        with self.assertRaises(DisplayError) as caught:
            parse_xrandr_query(text)
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_IDENTITY)

    def test_forced_output_bypasses_the_heuristic(self):
        self.assertEqual(
            parse_xrandr_query(randr_query(external=True), "HDMI-1").name,
            "HDMI-1",
        )

    def test_disabled_output_has_no_active_rotation(self):
        text = (
            "Screen 0: minimum 8 x 8, current 1920 x 1080, maximum 32767 x 32767\n"
            "eDP-1 connected (normal left inverted right) 344mm x 215mm\n"
            "HDMI-1 connected 1920x1080+0+0 (normal left inverted right) 527mm x 296mm\n"
        )
        self.assertEqual(parse_xrandr_query(text).name, "HDMI-1")


class X11TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="posture-x11-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "display.json"
        self.journal = Journal(self.path)

    def display(self, runner, touch=None, **environ):
        return X11Display(
            runner,
            self.journal,
            lambda text: text,
            sleep=lambda _s: None,
            environ=environ,
            touch=touch,
        )

    def test_tent_rotates_and_journals(self):
        runner = FakeXrandr()
        display = self.display(runner)
        display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(runner.rotation, "inverted")
        document = self.journal.load()
        self.assertEqual(document["backend"], "x11")
        self.assertEqual(document["spec"], ["eDP-1"])
        self.assertEqual(document["original_transform"], 0)
        self.assertEqual(document["written_transform"], 2)
        self.assertIsNone(document["written_lock"])

    def test_tablet_and_laptop_return_to_landscape(self):
        runner = FakeXrandr(rotation="inverted")
        display = self.display(runner)
        display.apply(Target(mode="tablet", tablet=None, rotation=None, disable_internal_input=True))
        self.assertEqual(runner.rotation, "normal")
        display.apply(Target(mode="laptop", tablet=False, rotation=0, disable_internal_input=False))
        self.assertEqual(runner.rotation, "normal")
        self.assertIsNotNone(self.journal.load())
        display.apply(Target(mode="hardware"))
        self.assertEqual(runner.rotation, "inverted")
        self.assertIsNone(self.journal.load())

    def test_restore_recovers_original_orientation(self):
        runner = FakeXrandr(rotation="left")
        display = self.display(runner)
        display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(runner.rotation, "inverted")
        display.apply(Target(mode="hardware"))
        self.assertEqual(runner.rotation, "left")
        self.assertIsNone(self.journal.load())

    def test_rotation_failure_rolls_back(self):
        runner = FakeXrandr(fail_rotate=2)
        display = self.display(runner)
        with self.assertRaises(DisplayError) as caught:
            display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        self.assertEqual(runner.rotation, "normal")
        self.assertIsNone(self.journal.load())

    def test_readback_mismatch_fails_after_retry(self):
        runner = FakeXrandr(ignore_rotate=True)
        display = self.display(runner)
        with self.assertRaises(DisplayError) as caught:
            display.apply(Target(mode="tent", tablet=True, rotation=180, disable_internal_input=True))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        rotations = [call for call in runner.calls if call[:2] == ["xrandr", "--output"]]
        self.assertEqual(len(rotations), 2)

    def test_missing_xrandr_is_a_stable_error(self):
        def missing(_args, timeout):
            del timeout
            raise FileNotFoundError("xrandr")

        display = self.display(missing)
        with self.assertRaises(DisplayError) as caught:
            display.apply(Target(mode="tent"))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)

    def test_touch_mapping_is_applied_after_rotation_and_journaled(self):
        runner = FakeXrandr()
        touch = FakeTouch()
        display = self.display(runner, touch)
        display.apply(Target(mode="tent"))
        self.assertEqual(runner.rotation, "inverted")
        self.assertEqual(touch.mapped, [(touch.snapshot, "eDP-1")])
        document = self.journal.load()
        self.assertEqual(document["original_auxiliary"], touch.snapshot)
        self.assertTrue(document["written_auxiliary"])

    def test_touch_capture_becoming_available_is_adopted_by_the_open_journal(self):
        runner = FakeXrandr()
        touch = FakeTouch()
        touch.capture_enabled = False
        display = self.display(runner, touch)
        display.apply(Target(mode="stand"))
        self.assertNotIn("original_auxiliary", self.journal.load())

        touch.capture_enabled = True
        display.apply(Target(mode="tent"))
        document = self.journal.load()
        self.assertEqual(document["original_auxiliary"], touch.snapshot)
        self.assertTrue(document["written_auxiliary"])
        self.assertEqual(runner.rotation, "inverted")

    def test_restore_returns_the_original_touch_matrix(self):
        runner = FakeXrandr(rotation="left")
        touch = FakeTouch()
        display = self.display(runner, touch)
        display.apply(Target(mode="tent"))
        display.apply(Target(mode="hardware"))
        self.assertEqual(runner.rotation, "left")
        self.assertEqual(touch.restored, [touch.snapshot])
        self.assertIsNone(self.journal.load())

    def test_touch_map_failure_rolls_back_rotation_and_touch(self):
        runner = FakeXrandr()
        touch = FakeTouch(fail_map=True)
        display = self.display(runner, touch)
        with self.assertRaises(DisplayError) as caught:
            display.apply(Target(mode="tent"))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        self.assertEqual(runner.rotation, "normal")
        self.assertEqual(touch.restored, [touch.snapshot])
        self.assertIsNone(self.journal.load())

    def test_laptop_restores_touch_after_a_software_transaction(self):
        runner = FakeXrandr()
        touch = FakeTouch()
        display = self.display(runner, touch)
        display.apply(Target(mode="tent"))
        touch.restored.clear()
        display.apply(Target(mode="laptop"))
        self.assertEqual(runner.rotation, "normal")
        self.assertEqual(touch.restored, [touch.snapshot])
        self.assertIsNotNone(self.journal.load())
        display.apply(Target(mode="hardware"))
        self.assertIsNone(self.journal.load())

    def test_first_laptop_entry_captures_touch_and_keeps_ownership(self):
        runner = FakeXrandr()
        touch = FakeTouch()
        display = self.display(runner, touch)
        display.apply(Target(mode="laptop"))
        self.assertEqual(touch.restored, [])
        self.assertIsNotNone(self.journal.load())
        display.apply(Target(mode="hardware"))
        self.assertIsNone(self.journal.load())

    def test_laptop_then_tent_maps_touch_again(self):
        runner = FakeXrandr()
        touch = FakeTouch()
        display = self.display(runner, touch)
        display.apply(Target(mode="tent"))
        display.apply(Target(mode="laptop"))
        display.apply(Target(mode="tent"))
        self.assertEqual(touch.mapped, [(touch.snapshot, "eDP-1")] * 2)
        self.assertTrue(self.journal.load()["written_auxiliary"])

    def test_startup_recovery_restores_journaled_touch_mapping(self):
        runner = FakeXrandr(rotation="inverted")
        touch = FakeTouch()
        original = touch.snapshot
        self.journal.save(
            {
                "version": 1,
                "backend": "x11",
                "spec": ["eDP-1"],
                "original_transform": 0,
                "original_lock": True,
                "written_lock": None,
                "written_transform": 2,
                "original_auxiliary": original,
                "written_auxiliary": True,
            }
        )
        display = self.display(runner, touch)
        self.assertEqual(runner.rotation, "normal")
        self.assertEqual(touch.restored, [original])
        self.assertIsNone(self.journal.load())

    def test_touch_restore_failure_keeps_the_journal_for_retry(self):
        runner = FakeXrandr(rotation="left")
        touch = FakeTouch(fail_restore=True)
        display = self.display(runner, touch)
        display.apply(Target(mode="tent"))
        with self.assertRaises(DisplayError) as caught:
            display.apply(Target(mode="hardware"))
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_TRANSFORM)
        self.assertEqual(runner.rotation, "left")
        self.assertIsNotNone(self.journal.load())
        self.assertIsNotNone(display.owned)


if __name__ == "__main__":
    unittest.main()
