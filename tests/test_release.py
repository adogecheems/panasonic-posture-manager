"""Release metadata consistency checks."""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CODE_ROOTS = (ROOT / "src", ROOT / "packaging", ROOT / "tests")
CODE_SUFFIXES = {".c", ".h", ".js", ".ps1", ".py", ".sh"}
INLINE_XML = re.compile(
    r"<\?xml\b|</?(?:arg|busconfig|Component(?:Group|Ref)?|Directory|"
    r"DirectoryRef|File|Fragment|interface|method|node|RemoveFolder|"
    r"ServiceControl|ServiceInstall|Shortcut(?:Property)?|signal|Wix)\b"
)
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
        from panasonic_posture.ui.settings import APP_VERSION
        self.assertEqual(APP_VERSION, self.project_version)

    def test_packaging_metadata_matches_the_release(self) -> None:
        self.assertIn(f"当前版本：{self.project_version}", self.readme)
        self.assertIn(
            f'"Project-Id-Version: panasonic-posture-manager '
            f'{self.project_version}\\n"',
            read("po/zh_CN.po"),
        )
        usage = read("MAINTENANCE.md")
        self.assertIn("panasonic-posture-manager", usage)
        self.assertIn("panasonic-posture-manager-bin", usage)

    def test_plasma_support_is_declared_experimental(self) -> None:
        self.assertIn("KDE Plasma 6", self.readme)
        self.assertIn("尚未实机验证", self.readme)
        self.assertIn("KDE Plasma 6 目前为实验性支持", self.readme)
        usage = read("MAINTENANCE.md")
        self.assertIn("KDE Plasma 6 Wayland 为实验性支持", usage)
        self.assertIn("尚未完成真实 Plasma 会话或真机验收", usage)

    def test_readme_avoids_maintainer_details(self) -> None:
        for detail in (
            "Wayland",
            "x86_64",
            "CFQV",
            "CF-QV",
            "MSI",
            "zh-CN",
            "en-US",
            "D-Bus",
            "RPC",
            "CSM",
            "ETW",
            "LocalSystem",
        ):
            with self.subTest(detail=detail):
                self.assertNotIn(
                    detail,
                    self.readme,
                    "README must describe user outcomes, not implementation details",
                )

    def test_release_inputs_exist(self) -> None:
        for relative in (
            "README.md",
            "LICENSE",
            "packaging/aur/PKGBUILD",
            "packaging/aur/.SRCINFO",
            "packaging/aur/panasonic-posture-manager.install",
            "packaging/windows/build_msi.py",
            "packaging/windows/msi/Package.wxs",
            "packaging/windows/msi/LICENSE.rtf",
            "packaging/windows/msi/main-path-actions.js",
            "packaging/windows/msi/Setup.zh-CN.wxl",
            "packaging/windows/msi/Setup.en-US.wxl",
            "src/panasonic_posture/resources/dbus.xml",
            "MAINTENANCE.md",
        ):
            with self.subTest(relative=relative):
                self.assertTrue((ROOT / relative).is_file(), relative)

    def test_xml_documents_are_not_embedded_in_code(self):
        for root in CODE_ROOTS:
            for path in root.rglob("*"):
                if path.suffix.lower() not in CODE_SUFFIXES or not path.is_file():
                    continue
                with self.subTest(path=path.relative_to(ROOT)):
                    match = INLINE_XML.search(path.read_text(encoding="utf-8"))
                    self.assertIsNone(
                        match,
                        "XML documents belong in resource files or must be "
                        "constructed through the XML API",
                    )

    def test_release_placeholder_is_not_left_in_packaging(self) -> None:
        self.assertNotIn("TODO(RELEASE)", self.pkgbuild)


if __name__ == "__main__":
    unittest.main()
