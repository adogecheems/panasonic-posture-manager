"""Platform layout contracts for native and packaging assets."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def payload_directories(directory: Path) -> set[str]:
    """Return immediate directories that contain non-cache payload files."""

    found: set[str] = set()
    for child in directory.iterdir():
        if not child.is_dir():
            continue
        if any(
            "__pycache__" not in path.parts
            for path in child.rglob("*")
            if path.is_file()
        ):
            found.add(child.name)
    return found


class RepositoryLayoutTests(unittest.TestCase):
    def test_native_backend_is_split_by_platform(self):
        backend = ROOT / "backend"
        self.assertEqual(payload_directories(backend), {"linux", "windows"})
        self.assertFalse(list(backend.glob("*.c")))
        self.assertFalse(list(backend.glob("*.h")))
        self.assertTrue((backend / "linux" / "relay.c").is_file())
        self.assertTrue((backend / "linux" / "tests" / "relay_test.c").is_file())
        self.assertTrue((backend / "windows" / "control.c").is_file())
        self.assertTrue(
            (backend / "windows" / "tests" / "control_test.c").is_file()
        )

    def test_packaging_is_split_into_common_and_platform_trees(self):
        packaging = ROOT / "packaging"
        self.assertEqual(
            payload_directories(packaging),
            {"common", "linux", "windows"},
        )
        self.assertTrue((packaging / "common" / "release_version.py").is_file())
        self.assertTrue((packaging / "linux" / "aur" / "PKGBUILD").is_file())
        self.assertTrue(
            (packaging / "linux" / "ci" / "aur_known_hosts").is_file()
        )
        self.assertFalse((packaging / "common" / "ci").exists())
        self.assertTrue(
            (
                packaging
                / "linux"
                / "systemd"
                / "panasonic-posture-manager.service"
            ).is_file()
        )
        self.assertTrue((packaging / "windows" / "build_msi.py").is_file())

    def test_runtime_assets_are_split_by_platform(self):
        assets = ROOT / "data" / "assets"
        self.assertEqual(payload_directories(assets), {"linux", "windows"})
        self.assertTrue((assets / "linux" / "laptop.svg").is_file())
        self.assertTrue(
            (assets / "windows" / "work.mmoe.panasonic.PostureManager.ico").is_file()
        )

    def test_platform_tests_have_explicit_names_and_homes(self):
        tests = ROOT / "tests"
        for name in (
            "test_linux_access.py",
            "test_linux_agent.py",
            "test_linux_display.py",
            "test_linux_protocol.py",
            "test_linux_ui_contract.py",
            "test_windows_optional_input.py",
            "test_windows_ui.py",
        ):
            with self.subTest(name=name):
                self.assertTrue((tests / name).is_file())
        for name in (
            "test_access.py",
            "test_agent.py",
            "test_display.py",
            "test_protocol.py",
            "test_ui_contract.py",
            "test_fluent.py",
            "test_optional_input.py",
        ):
            with self.subTest(removed=name):
                self.assertFalse((tests / name).exists())
        self.assertEqual(
            payload_directories(tests / "manual"),
            {"linux", "windows"},
        )

    def test_build_entrypoints_use_the_split_paths(self):
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("backend/linux/relay", makefile)
        self.assertIn("backend/linux/tests/relay_test.c", makefile)
        self.assertIn("packaging/linux/scripts/build-deb.sh", makefile)
        self.assertIn("packaging/linux/scripts/build-rpm.sh", makefile)

        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("packaging/linux/aur/PKGBUILD", workflow)
        self.assertIn("packaging/common/release_version.py", workflow)
        self.assertIn("packaging/windows/build_msi.py", workflow)
        windows = (
            ROOT / ".github" / "workflows" / "windows.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("panasonic_posture/fluent", windows)
        self.assertTrue(
            (ROOT / ".github" / "workflows" / "linux.yml").is_file()
        )


if __name__ == "__main__":
    unittest.main()
