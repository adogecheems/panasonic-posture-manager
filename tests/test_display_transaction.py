"""Shared display ownership and recovery rules without platform I/O."""

from dataclasses import replace
import unittest

from panasonic_posture.model import Target
from panasonic_posture.session.failures import DisplayError, ErrorCode
from panasonic_posture.session.transaction import DisplayState, SoftwareDisplay


class MemoryJournal:
    def __init__(self):
        self.document = None
        self.reject_laptop_baseline = False

    def load(self):
        return None if self.document is None else self.document.copy()

    def save(self, document):
        if self.reject_laptop_baseline and document["original_transform"] == 0:
            raise OSError("journal write failed")
        self.document = document.copy()

    def clear(self):
        self.document = None


class Display(SoftwareDisplay):
    backend_name = "fixture"

    def __init__(self, state, journal):
        self.state = state
        self.rotations = []
        self.fail_rotation = False
        super().__init__(journal)

    def _read_state(self):
        return self.state

    def _write_rotation(self, state, transform):
        self.rotations.append(transform)
        self.state = replace(self.state, transform=transform)
        if self.fail_rotation:
            self.fail_rotation = False
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM, "readback failed")

    def _write_lock(self, state, locked):
        self.state = replace(self.state, lock=locked)


class LaptopRecoveryTests(unittest.TestCase):
    def make(self, transform=2, locked=False):
        journal = MemoryJournal()
        display = Display(DisplayState(("panel",), transform, locked, True), journal)
        return display, journal

    def test_verified_laptop_is_the_direction_restored_after_release_or_crash(self):
        for original in (1, 2, 3):
            for locked in (False, True):
                for finish in ("release", "crash"):
                    with self.subTest(original=original, locked=locked, finish=finish):
                        display, journal = self.make(original, locked)
                        display.apply(Target(mode="laptop"))
                        display.rotations.clear()

                        if finish == "release":
                            display.apply(Target(mode="unavailable"))
                        else:
                            display = Display(display.state, journal)

                        self.assertEqual(display.state.transform, 0)
                        self.assertEqual(display.state.lock, locked)
                        self.assertEqual(display.rotations, [])
                        self.assertIsNone(journal.load())

    def test_laptop_replaces_an_older_tent_or_native_tablet_direction(self):
        for previous in ("tent", "hardware", "hold"):
            with self.subTest(previous=previous):
                display, journal = self.make()
                display.apply(Target(mode="tent"))
                if previous != "tent":
                    display.apply(Target(mode="hardware"))
                    display.state = replace(display.state, transform=0)
                if previous == "hold":
                    display.state = replace(display.state, transform=1)
                    display.hold_orientation()
                display.apply(Target(mode="laptop"))
                display.apply(Target(mode="tent"))

                display.restore()

                self.assertEqual(display.state.transform, 0)
                self.assertFalse(display.state.lock)
                self.assertIsNone(journal.load())

    def test_failed_laptop_keeps_the_previous_recovery_direction(self):
        for failure in ("rotation", "journal"):
            with self.subTest(failure=failure):
                display, journal = self.make(transform=1)
                display.apply(Target(mode="tent"))
                display.fail_rotation = failure == "rotation"
                journal.reject_laptop_baseline = failure == "journal"

                exception = DisplayError if failure == "rotation" else OSError
                with self.assertRaises(exception):
                    display.apply(Target(mode="laptop"))

                self.assertEqual(display.state.transform, 1)
                self.assertFalse(display.state.lock)
                self.assertIsNone(journal.load())

    def test_tent_and_stand_alone_preserve_the_original_recovery_direction(self):
        for mode in ("tent", "stand"):
            with self.subTest(mode=mode):
                display, journal = self.make(transform=1)
                display.apply(Target(mode=mode))

                display.restore()

                self.assertEqual(display.state.transform, 1)
                self.assertFalse(display.state.lock)
                self.assertIsNone(journal.load())

    def test_restore_preserves_a_later_external_direction(self):
        display, journal = self.make()
        display.apply(Target(mode="laptop"))
        display.state = replace(display.state, transform=3)
        display.rotations.clear()

        display.restore()

        self.assertEqual(display.state.transform, 3)
        self.assertEqual(display.rotations, [])
        self.assertIsNone(journal.load())


if __name__ == "__main__":
    unittest.main()
