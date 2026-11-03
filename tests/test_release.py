"""Release metadata consistency checks."""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from panasonic_posture import __version__  # noqa: E402


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def match(pattern: str, text: str, label: str) -> str:
    found = re.search(pattern, text, re.MULTILINE)
    if found is None:
        raise AssertionError(f"cannot find {label}")
    return found.group(1)


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        project = tomllib.loads(read("pyproject.toml"))
        self.project_version = str(project["project"]["version"])
        self.pkgbuild = read("packaging/aur/PKGBUILD")
        self.srcinfo = read("packaging/aur/.SRCINFO")
        self.readme = read("README.md")
        self.changelog = read("CHANGELOG.md")

    def test_python_package_and_project_versions_match(self) -> None:
        self.assertEqual(self.project_version, __version__)

    def test_arch_package_version_and_source_tag_match(self) -> None:
        pkgver = match(r"^pkgver=(\S+)$", self.pkgbuild, "PKGBUILD pkgver")
        srcinfo_version = match(
            r"^\s+pkgver = (\S+)$", self.srcinfo, ".SRCINFO pkgver"
        )
        srcinfo_tag = match(
            r"^\s+source = .*#tag=v(\S+)$",
            self.srcinfo,
            ".SRCINFO source tag",
        )
        self.assertIn("#tag=v${pkgver}", self.pkgbuild)
        self.assertEqual(
            {self.project_version, pkgver, srcinfo_version, srcinfo_tag},
            {self.project_version},
        )

    def test_ui_about_version_matches_release(self) -> None:
        settings = read("data/ui/settingsPage.js")
        ui_version = match(
            r"^export const APP_VERSION = '([^']+)';$",
            settings,
            "UI APP_VERSION",
        )
        self.assertEqual(ui_version, self.project_version)

    def test_readme_and_changelog_announce_current_release(self) -> None:
        self.assertIn(f"当前版本：{self.project_version}", self.readme)
        self.assertIn(f"## [{self.project_version}] - ", self.changelog)

    def test_install_and_packaging_examples_pin_the_release(self) -> None:
        self.assertIn(f"git clone --branch v{self.project_version}", self.readme)
        self.assertIn(
            f'"Project-Id-Version: panasonic-posture-manager '
            f'{self.project_version}\\n"',
            read("po/zh_CN.po"),
        )
        self.assertIn(
            f"panasonic-posture-manager-{self.project_version}-1-x86_64",
            read("packaging/aur/README.md"),
        )

    def test_release_documents_and_packaging_inputs_exist(self) -> None:
        for relative in (
            "README.md",
            "CHANGELOG.md",
            "LICENSE",
            "packaging/aur/PKGBUILD",
            "packaging/aur/.SRCINFO",
            "packaging/aur/panasonic-posture-manager.install",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file(), relative)

    def test_release_placeholder_is_not_left_in_packaging(self) -> None:
        self.assertNotIn("TODO(RELEASE)", self.pkgbuild)


if __name__ == "__main__":
    unittest.main()
