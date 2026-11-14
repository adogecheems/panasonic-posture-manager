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
            (
                packaging
                / "linux"
                / "systemd"
                / "panasonic-posture-manager.service"
            ).is_file()
        )
        self.assertTrue((packaging / "windows" / "build_msi.py").is_file())

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


if __name__ == "__main__":
    unittest.main()
