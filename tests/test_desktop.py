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
from panasonic_posture.session.x11 import X11Display
from panasonic_posture.session.x11_touch import X11TouchMapping


class DetectionTests(unittest.TestCase):
    def test_gnome_wayland_and_x11_use_the_mutter_backend(self):
        for session_type in ("wayland", "x11"):
            with self.subTest(session_type=session_type):
                self.assertEqual(
                    detect_backend(
                        {
                            "XDG_CURRENT_DESKTOP": "ubuntu:GNOME",
                            "XDG_SESSION_TYPE": session_type,
                        }
                    ),
                    "gnome",
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

    def test_plasma_x11_uses_xrandr(self):
        self.assertEqual(
            detect_backend(
                {
                    "XDG_CURRENT_DESKTOP": "KDE",
                    "XDG_SESSION_TYPE": "x11",
                    "KDE_SESSION_VERSION": "6",
                }
            ),
            "x11",
        )

    def test_other_x11_desktops(self):
        self.assertEqual(
            detect_backend({"XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "x11"}),
            "x11",
        )
        self.assertEqual(detect_backend({"DISPLAY": ":0"}), "x11")

    def test_unknown_wayland_is_unsupported(self):
        self.assertEqual(
            detect_backend({"XDG_CURRENT_DESKTOP": "sway", "XDG_SESSION_TYPE": "wayland"}),
            "unsupported",
        )

    def test_explicit_override(self):
        self.assertEqual(
            detect_backend(
                {
                    "PANASONIC_POSTURE_DISPLAY_BACKEND": "x11",
                    "XDG_CURRENT_DESKTOP": "GNOME",
                    "XDG_SESSION_TYPE": "wayland",
                }
            ),
            "x11",
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

    def test_x11_factory(self):
        display = create_display(
            None,
            self.path,
            lambda text: text,
            environ={"XDG_CURRENT_DESKTOP": "XFCE", "XDG_SESSION_TYPE": "x11"},
        )
        self.assertIsInstance(display, X11Display)
        self.assertIsInstance(display._touch, X11TouchMapping)

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
