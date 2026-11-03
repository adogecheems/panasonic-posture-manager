"""Tests for desktop detection and display backend selection."""

import tempfile
import unittest
from pathlib import Path

from panasonic_posture.protocol import ErrorCode
from panasonic_posture.session.desktop import (
    UnsupportedDisplay,
    create_display,
    detect_backend,
)
from panasonic_posture.session.display import Display
from panasonic_posture.session.errors import DisplayError
from panasonic_posture.session.plasma import KScreenCtlCommand, PlasmaDisplay


class DetectionTests(unittest.TestCase):
    def test_gnome_wayland_uses_the_mutter_backend(self):
        self.assertEqual(
            detect_backend(
                {
                    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
                    "XDG_SESSION_TYPE": "wayland",
                }
            ),
            "gnome",
        )

    def test_gnome_x11_is_unsupported(self):
        self.assertEqual(
            detect_backend(
                {
                    "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
                    "XDG_SESSION_TYPE": "x11",
                    "DISPLAY": ":0",
                }
            ),
            "unsupported",
        )

    def test_plasma_6_wayland(self):
        self.assertEqual(
            detect_backend(
                {
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_TYPE": "wayland",
                    "KDE_SESSION_VERSION": "6",
                }
            ),
            "plasma",
        )

    def test_plasma_5_wayland_is_not_advertised_as_plasma_6(self):
        self.assertEqual(
            detect_backend(
                {
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_TYPE": "wayland",
                    "KDE_SESSION_VERSION": "5",
                }
            ),
            "unsupported",
        )

    def test_plasma_x11_is_unsupported(self):
        self.assertEqual(
            detect_backend(
                {
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_TYPE": "x11",
                    "KDE_SESSION_VERSION": "6",
                }
            ),
            "unsupported",
        )

    def test_other_x11_desktops_are_unsupported(self):
        self.assertEqual(
            detect_backend({"XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "x11"}),
            "unsupported",
        )
        self.assertEqual(detect_backend({"DISPLAY": ":0"}), "unsupported")

    def test_unknown_wayland_is_unsupported(self):
        self.assertEqual(
            detect_backend({"XDG_CURRENT_DESKTOP": "sway", "XDG_SESSION_TYPE": "wayland"}),
            "unsupported",
        )

    def test_explicit_override(self):
        self.assertEqual(
            detect_backend(
                {
                    "PANASONIC_POSTURE_DISPLAY_BACKEND": "plasma",
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "XDG_SESSION_TYPE": "wayland",
                }
            ),
            "plasma",
        )

    def test_x11_cannot_be_forced_with_an_override(self):
        self.assertEqual(
            detect_backend(
                {
                    "PANASONIC_POSTURE_DISPLAY_BACKEND": "x11",
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "XDG_SESSION_TYPE": "wayland",
                }
            ),
            "unsupported",
        )


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="posture-desktop-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "display.json"

    def test_gnome_factory(self):
        display = create_display(
            None,
            self.path,
            lambda text: text,
            environ={"XDG_CURRENT_DESKTOP": "GNOME", "XDG_SESSION_TYPE": "wayland"},
        )
        self.assertIsInstance(display, Display)

    def test_x11_session_gets_a_stable_unsupported_backend(self):
        display = create_display(
            None,
            self.path,
            lambda text: text,
            environ={"XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "x11"},
        )
        self.assertIsInstance(display, UnsupportedDisplay)
        with self.assertRaises(DisplayError) as caught:
            display.apply(None)
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)

    def test_plasma_factory_prefers_kscreenctl(self):
        display = create_display(
            None,
            self.path,
            lambda text: text,
            environ={
                "XDG_CURRENT_DESKTOP": "KDE",
                "XDG_SESSION_TYPE": "wayland",
                "KDE_SESSION_VERSION": "6",
            },
            which=lambda name: "/usr/bin/kscreenctl" if name == "kscreenctl" else None,
        )
        self.assertIsInstance(display, PlasmaDisplay)
        self.assertIsInstance(display.command, KScreenCtlCommand)

    def test_unsupported_session_does_not_crash_the_adapter(self):
        display = create_display(
            None,
            self.path,
            lambda text: text,
            environ={"XDG_CURRENT_DESKTOP": "sway", "XDG_SESSION_TYPE": "wayland"},
        )
        self.assertIsInstance(display, UnsupportedDisplay)
        with self.assertRaises(DisplayError) as caught:
            display.apply(None)
        self.assertEqual(caught.exception.code, ErrorCode.DISPLAY_UNAVAILABLE)


if __name__ == "__main__":
    unittest.main()
