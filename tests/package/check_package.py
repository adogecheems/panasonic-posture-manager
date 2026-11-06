#!/usr/bin/env python3
"""Acceptance checks for a built Arch package.

The checks use only the package archive and an isolated pacman root.  They do
not install into the host, start system services or touch real devices.  The
package is still validated end-to-end at the packaging layer: metadata, exact
file layout, file content, service configuration, a clean install, a reinstall
or upgrade, and removal.
"""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_NAME = "panasonic-posture-manager"
INTERFACE = "work.mmoe.panasonic.Posture"
META_FILES = {".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL"}
SITE_PACKAGES = re.compile(r"^usr/lib/python3\.\d+/site-packages/")
PYTHON_CACHE = re.compile(
    r"^(?P<module>.+)/__pycache__/(?P<stem>[^/]+)\.cpython-\d+"
    r"(?:\.opt-\d+)?\.pyc$"
)
TEXT_SUFFIXES = (
    ".py",
    ".js",
    ".json",
    ".svg",
    ".service",
    ".conf",
    ".policy",
    ".desktop",
    ".txt",
    ".install",
)


class PackageAcceptance:
    def __init__(
        self,
        package: Path,
        *,
        source_root: Path,
        previous: Path | None,
        install: bool,
    ) -> None:
        self.package = package.resolve()
        self.source_root = source_root.resolve()
        self.previous = previous.resolve() if previous else None
        self.run_install = install
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.passes = 0
        self.temp_root: Path | None = None
        self.extract_root: Path | None = None
        self.files: dict[str, Path] = {}
        self.pkg_info: dict[str, list[str]] = {}
        self.project: dict[str, object] = {}
        self.pkgver = ""
        self.pkgrel = ""
        self.version = ""
        self.site_dir = ""
        self.srcinfo: dict[str, list[tuple[str, dict[str, list[str]]]]] = {}
        self.srcinfo_text = ""

    @contextmanager
    def stage(self, label: str) -> Iterator[None]:
        before = len(self.errors)
        yield
        if len(self.errors) == before:
            self.passes += 1
            print(f"PASS  {label}")

    def fail(self, message: str) -> None:
        self.errors.append(message)
        print(f"FAIL  {message}", file=sys.stderr, flush=True)

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"WARN  {message}", file=sys.stderr)

    def require(self, condition: bool, message: str) -> bool:
        if not condition:
            self.fail(message)
        return condition

    def run_checked(
        self,
        command: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                env=env,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as error:
            self.fail(f"cannot run {command[0]}: {error}")
            return None

    def archive_entries(self, archive: Path) -> list[str]:
        result = self.run_checked(["bsdtar", "-tf", str(archive)])
        if result is None or result.returncode != 0:
            if result is not None:
                self.fail(f"cannot list {archive}: {result.stdout.strip()}")
            return []
        return [line for line in result.stdout.splitlines() if line]

    def archive_file(self, archive: Path, name: str) -> str:
        result = self.run_checked(["bsdtar", "-xOf", str(archive), name])
        if result is None or result.returncode != 0:
            if result is not None:
                self.fail(f"cannot read {name} from {archive}: {result.stdout.strip()}")
            return ""
        return result.stdout

    def check_archive(self) -> None:
        if not self.package.is_file():
            self.fail(f"package archive not found: {self.package}")
            return

        expected_tools = ["bsdtar"]
        if self.run_install:
            expected_tools.append("pacman")
        missing_tools = [tool for tool in expected_tools if shutil.which(tool) is None]
        if missing_tools:
            self.fail(f"missing required tool(s): {', '.join(missing_tools)}")
            return

        entries = self.archive_entries(self.package)
        if not entries:
            self.fail("package archive is empty or unreadable")
            return
        missing_meta = sorted(META_FILES - set(entries))
        self.require(not missing_meta, f"missing package metadata: {', '.join(missing_meta)}")

        with self.stage("archive structure"):
            for entry in entries:
                normalized = entry.rstrip("/")
                if normalized in META_FILES or normalized == "usr":
                    continue
                path = Path(normalized)
                if path.is_absolute() or ".." in path.parts:
                    self.fail(f"unsafe archive path: {entry}")
                elif not normalized.startswith("usr/"):
                    self.fail(f"unexpected path outside /usr: {entry}")
            if self.errors:
                return

        assert self.temp_root is not None
        self.extract_root = self.temp_root / "root"
        self.extract_root.mkdir()
        result = self.run_checked(
            ["bsdtar", "-xf", str(self.package), "-C", str(self.extract_root)]
        )
        if result is None or result.returncode != 0:
            if result is not None:
                self.fail(f"cannot extract package: {result.stdout.strip()}")
            return

        for path in self.extract_root.rglob("*"):
            if path.is_symlink():
                self.fail(f"package contains a symlink: {path.relative_to(self.extract_root)}")
            elif not (path.is_file() or path.is_dir()):
                self.fail(
                    f"package contains an unsupported file type: "
                    f"{path.relative_to(self.extract_root)}"
                )

        self.files = {
            path.relative_to(self.extract_root).as_posix(): path
            for path in self.extract_root.rglob("*")
            if path.is_file()
            and path.relative_to(self.extract_root).as_posix() not in META_FILES
        }
        self.pkg_info = parse_key_values(self.archive_file(self.package, ".PKGINFO"))
        self.load_source_metadata()

    def load_source_metadata(self) -> None:
        pyproject_path = self.source_root / "pyproject.toml"
        srcinfo_path = self.source_root / "packaging/aur/.SRCINFO"
        package_dir = self.source_root / "packaging/aur"
        if not pyproject_path.is_file() or not srcinfo_path.is_file() or not package_dir.is_dir():
            self.fail(f"source metadata is incomplete under {self.source_root}")
            return

        project_file = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = project_file.get("project")
        if not isinstance(project, dict):
            self.fail("pyproject.toml has no [project] table")
            return
        self.project = project
        self.version = str(project.get("version", ""))
        self.srcinfo_text = srcinfo_path.read_text(encoding="utf-8")
        self.srcinfo = parse_srcinfo(self.srcinfo_text)

    def check_metadata(self) -> None:
        with self.stage("package and source metadata"):
            base_section = first_srcinfo_section(self.srcinfo, "pkgbase")
            pkg_sections = section_by_name(self.srcinfo, "pkgname", PACKAGE_NAME)
            if not self.require(base_section is not None, "missing pkgbase in .SRCINFO"):
                return
            if not self.require(pkg_sections is not None, "missing package in .SRCINFO"):
                return
            assert base_section is not None and pkg_sections is not None

            self.pkgver = one_value(base_section, "pkgver", self)
            self.pkgrel = one_value(base_section, "pkgrel", self)
            expected_pkgver = f"{self.pkgver}-{self.pkgrel}"
            self.require(self.pkgver == self.version, ".SRCINFO and pyproject versions differ")
            self.require(
                one_value(self.pkg_info, "pkgver", self) == expected_pkgver,
                f"package version is not {expected_pkgver}",
            )
            self.require(
                one_value(self.pkg_info, "pkgname", self) == PACKAGE_NAME,
                f"unexpected package name; expected {PACKAGE_NAME}",
            )
            self.require(
                one_value(self.pkg_info, "pkgbase", self) == PACKAGE_NAME,
                "package pkgbase does not match the source package",
            )
            self.require(
                "pkgtype=debug" not in " ".join(self.pkg_info.get("xdata", [])),
                "a -debug package was supplied; pass the main package instead",
            )

            for key in ("pkgdesc", "url", "arch", "license"):
                self.require(
                    sorted(self.pkg_info.get(key, [])) == sorted(base_section.get(key, [])),
                    f"package {key} does not match .SRCINFO",
                )
            for key, source_key in (("depend", "depends"), ("optdepend", "optdepends")):
                self.require(
                    sorted(self.pkg_info.get(key, []))
                    == sorted(base_section.get(source_key, [])),
                    f"package {key} values do not match .SRCINFO",
                )

            self.require(
                self.project.get("name") == PACKAGE_NAME,
                "pyproject project name does not match the package",
            )
            self.require(
                self.project.get("license") == one_value(self.pkg_info, "license", self),
                "pyproject license does not match the package",
            )
            self.require(
                any(
                    f"#tag=v{self.version}" in source
                    for source in base_section.get("source", [])
                ),
                "PKGBUILD source does not pin the pyproject version tag",
            )
            install_name = one_value(base_section, "install", self)
            install_source = self.source_root / "packaging/aur" / install_name
            self.require(install_source.is_file(), f"install script not found: {install_source}")
            if install_source.is_file():
                self.require(
                    self.archive_file(self.package, ".INSTALL") == install_source.read_text(
                        encoding="utf-8"
                    ),
                    "packaged .INSTALL differs from packaging/aur install script",
                )

            scripts = self.project.get("scripts")
            if not isinstance(scripts, dict):
                self.fail("pyproject has no [project.scripts] table")

    def check_layout(self) -> None:
        with self.stage("file layout and source parity"):
            if not self.require(bool(self.files), "package contains no files"):
                return

            self.require(
                all(path.startswith(("usr/bin/", "usr/lib/", "usr/share/")) for path in self.files),
                "package contains a file outside the expected /usr prefixes",
            )
            self.require(
                not any("usr/local" in path.split("/") for path in self.files),
                "package contains a /usr/local path",
            )

            site_roots = sorted(
                {match.group(0) for path in self.files if (match := SITE_PACKAGES.match(path))}
            )
            if not self.require(len(site_roots) == 1, "expected exactly one site-packages root"):
                return
            self.site_dir = site_roots[0].rstrip("/")

            expected_copies = self.expected_source_copies()
            for package_path, source_path in expected_copies.items():
                packaged = self.files.get(package_path)
                if not self.require(packaged is not None, f"missing packaged file: {package_path}"):
                    continue
                if not self.require(source_path.is_file(), f"missing source file: {source_path}"):
                    continue
                self.require(
                    packaged.read_bytes() == source_path.read_bytes(),
                    f"packaged file differs from source: {package_path}",
                )

            package_data = {
                path
                for path in self.files
                if path.startswith("usr/share/panasonic-posture-manager/")
            }
            source_data = {
                path
                for path in expected_copies
                if path.startswith("usr/share/panasonic-posture-manager/")
            }
            self.require(
                package_data == source_data,
                "data file set differs from data/: "
                f"missing={sorted(source_data - package_data)}, "
                f"extra={sorted(package_data - source_data)}",
            )

            source_modules = {
                path.relative_to(self.source_root / "src/panasonic_posture").as_posix()
                for path in (self.source_root / "src/panasonic_posture").rglob("*.py")
            }
            module_prefix = f"{self.site_dir}/panasonic_posture/"
            packaged_modules = {
                path[len(module_prefix) :]
                for path in self.files
                if path.startswith(module_prefix)
                and path.endswith(".py")
                and "/__pycache__/" not in path
            }
            self.require(
                packaged_modules == source_modules,
                "site-packages module set differs from src/: "
                f"missing={sorted(source_modules - packaged_modules)}, "
                f"extra={sorted(packaged_modules - source_modules)}",
            )
            for module in sorted(source_modules):
                packaged = self.files.get(module_prefix + module)
                source = self.source_root / "src/panasonic_posture" / module
                if packaged is not None:
                    self.require(
                        packaged.read_bytes() == source.read_bytes(),
                        f"packaged module differs from source: {module}",
                    )

            dist_info = (
                f"{self.site_dir}/panasonic_posture_manager-{self.version}.dist-info"
            )
            expected_dist = {
                "METADATA",
                "RECORD",
                "WHEEL",
                "entry_points.txt",
                "top_level.txt",
                "licenses/LICENSE",
            }
            actual_dist = {
                path[len(dist_info) + 1 :]
                for path in self.files
                if path.startswith(dist_info + "/")
            }
            self.require(
                actual_dist == expected_dist,
                "wheel metadata file set is incomplete or unexpected: "
                f"missing={sorted(expected_dist - actual_dist)}, "
                f"extra={sorted(actual_dist - expected_dist)}",
            )

            for path in self.files:
                cache = PYTHON_CACHE.match(path)
                if not cache:
                    if path.endswith(".pyc") and "/__pycache__/" not in path:
                        self.fail(f"bytecode outside __pycache__: {path}")
                    continue
                source = f"{cache.group('module')}/{cache.group('stem')}.py"
                self.require(
                    source in self.files,
                    f"bytecode has no packaged source: {path}",
                )

            metadata_path = self.files.get(f"{dist_info}/METADATA")
            if metadata_path is not None:
                metadata = metadata_path.read_text(encoding="utf-8", errors="replace")
                self.require(
                    f"Version: {self.version}\n" in metadata,
                    "wheel METADATA version differs from the package version",
                )
                self.require(
                    "License-Expression: AGPL-3.0-or-later\n" in metadata,
                    "wheel METADATA license is not AGPL-3.0-or-later",
                )

            entry_points = self.files.get(f"{dist_info}/entry_points.txt")
            expected_scripts = self.project.get("scripts")
            if entry_points is not None and isinstance(expected_scripts, dict):
                text = entry_points.read_text(encoding="utf-8")
                for name, target in expected_scripts.items():
                    self.require(
                        f"{name} = {target}\n" in text,
                        f"wheel entry point is missing or wrong: {name}",
                    )

            for name in ("cfqv1.json", "cfqv8.json", "cfqv9.json"):
                profile = self.files.get(
                    f"usr/share/panasonic-posture-manager/profiles/{name}"
                )
                if not self.require(profile is not None, f"missing profile: {name}"):
                    continue
                try:
                    json.loads(profile.read_text(encoding="utf-8"))
                except (OSError, ValueError) as error:
                    self.fail(f"invalid profile JSON {name}: {error}")

            locale = self.files.get(
                "usr/share/locale/zh_CN/LC_MESSAGES/panasonic-posture-manager.mo"
            )
            if self.require(locale is not None, "Chinese gettext catalog is missing"):
                assert locale is not None
                self.require(
                    locale.read_bytes().startswith(b"\xde\x12\x04\x95"),
                    "Chinese gettext catalog has an invalid MO signature",
                )

    def expected_source_copies(self) -> dict[str, Path]:
        copies: dict[str, Path] = {}
        data_root = self.source_root / "data"
        for source in data_root.rglob("*"):
            if source.is_file():
                copies[
                    f"usr/share/panasonic-posture-manager/{source.relative_to(data_root).as_posix()}"
                ] = source
        copies[
            "usr/share/icons/hicolor/512x512/apps/work.mmoe.panasonic.PostureManager.png"
        ] = data_root / "assets/work.mmoe.panasonic.PostureManager.png"
        copies.update(
            {
                "usr/lib/modules-load.d/panasonic-posture-manager.conf": self.source_root
                / "packaging/modules-load.d/uinput.conf",
                "usr/lib/systemd/system/panasonic-posture-manager.service": self.source_root
                / "packaging/systemd/panasonic-posture-manager.service",
                "usr/lib/systemd/user/panasonic-posture-manager-session.service": self.source_root
                / "packaging/systemd/user/panasonic-posture-manager-session.service",
                "usr/share/dbus-1/system-services/work.mmoe.panasonic.Posture.service": self.source_root
                / "packaging/dbus/work.mmoe.panasonic.Posture.service",
                "usr/share/dbus-1/system.d/work.mmoe.panasonic.Posture.conf": self.source_root
                / "packaging/dbus/work.mmoe.panasonic.Posture.conf",
                "usr/share/polkit-1/actions/work.mmoe.panasonic.Posture.policy": self.source_root
                / "packaging/polkit/work.mmoe.panasonic.Posture.policy",
                "usr/share/applications/work.mmoe.panasonic.PostureManager.desktop": self.source_root
                / "packaging/desktop/work.mmoe.panasonic.PostureManager.desktop",
                "usr/share/licenses/panasonic-posture-manager/LICENSE": self.source_root / "LICENSE",
            }
        )
        return copies

    def check_modes(self) -> None:
        with self.stage("executables and file modes"):
            executable = {
                "usr/bin/panasonic-posture-manager",
                "usr/bin/panasonic-posture-manager-probe",
                "usr/lib/panasonic-posture-manager/relay",
            }
            for path in sorted(executable):
                entry = self.files.get(path)
                if not self.require(entry is not None, f"missing executable: {path}"):
                    continue
                assert entry is not None
                self.require(
                    stat.S_IMODE(entry.stat().st_mode) & 0o111 != 0,
                    f"file is not executable: {path}",
                )

            for path, entry in self.files.items():
                if path in executable:
                    continue
                self.require(
                    stat.S_IMODE(entry.stat().st_mode) & 0o111 == 0,
                    f"unexpected executable mode: {path}",
                )

            for path in (
                "usr/bin/panasonic-posture-manager",
                "usr/bin/panasonic-posture-manager-probe",
            ):
                entry = self.files.get(path)
                if entry is not None:
                    self.require(
                        entry.read_bytes().startswith(b"#!/usr/bin/python3\n"),
                        f"unexpected console-script interpreter: {path}",
                    )

            relay = self.files.get("usr/lib/panasonic-posture-manager/relay")
            if relay is not None:
                self.require(
                    relay.read_bytes().startswith(b"\x7fELF"),
                    "packaged relay is not an ELF executable",
                )

            for path, entry in self.files.items():
                if entry.stat().st_size == 0:
                    self.fail(f"empty packaged file: {path}")
                if path.endswith(TEXT_SUFFIXES):
                    try:
                        text = entry.read_text(encoding="utf-8")
                    except UnicodeDecodeError:
                        continue
                    self.require(
                        "/usr/local/" not in text,
                        f"packaged text file contains /usr/local: {path}",
                    )

    def check_service_configuration(self) -> None:
        with self.stage("systemd, D-Bus, polkit and desktop files"):
            system_unit = self.files.get(
                "usr/lib/systemd/system/panasonic-posture-manager.service"
            )
            user_unit = self.files.get(
                "usr/lib/systemd/user/panasonic-posture-manager-session.service"
            )
            if self.require(system_unit is not None, "system service unit is missing"):
                assert system_unit is not None
                self.require(
                    "BusName=work.mmoe.panasonic.Posture" in system_unit.read_text(encoding="utf-8"),
                    "system unit has the wrong D-Bus name",
                )
                self.require(
                    "ExecStopPost=-/usr/lib/panasonic-posture-manager/relay --recover"
                    in system_unit.read_text(encoding="utf-8"),
                    "system unit does not reference the packaged relay",
                )
            if self.require(user_unit is not None, "user session unit is missing"):
                assert user_unit is not None
                self.require(
                    "ExecStart=/usr/bin/python3 -m panasonic_posture.session.agent"
                    in user_unit.read_text(encoding="utf-8"),
                    "user unit has the wrong session entry point",
                )

            if shutil.which("systemd-analyze"):
                for unit in (system_unit, user_unit):
                    if unit is None:
                        continue
                    result = self.run_checked(["systemd-analyze", "verify", str(unit)])
                    if result is not None:
                        self.require(
                            result.returncode == 0,
                            f"systemd-analyze rejected {unit.name}: {result.stdout.strip()}",
                        )
            else:
                self.warn("systemd-analyze is unavailable; unit syntax was only read")

            dbus_conf = self.files.get(
                "usr/share/dbus-1/system.d/work.mmoe.panasonic.Posture.conf"
            )
            polkit = self.files.get(
                "usr/share/polkit-1/actions/work.mmoe.panasonic.Posture.policy"
            )
            for path, entry in (("D-Bus policy", dbus_conf), ("polkit policy", polkit)):
                if not self.require(entry is not None, f"{path} is missing"):
                    continue
                assert entry is not None
                try:
                    ET.fromstring(entry.read_text(encoding="utf-8"))
                except ET.ParseError as error:
                    self.fail(f"invalid {path} XML: {error}")
            if polkit is not None:
                self.require(
                    "work.mmoe.panasonic.Posture.configure" in polkit.read_text(encoding="utf-8"),
                    "polkit policy is missing the configure action",
                )
            if shutil.which("xmllint"):
                for entry in (dbus_conf, polkit):
                    if entry is None:
                        continue
                    result = self.run_checked(["xmllint", "--noout", str(entry)])
                    if result is not None:
                        self.require(
                            result.returncode == 0,
                            f"xmllint rejected {entry.name}: {result.stdout.strip()}",
                        )
            else:
                self.warn("xmllint is unavailable; XML was parsed with ElementTree only")

            activation = self.files.get(
                "usr/share/dbus-1/system-services/work.mmoe.panasonic.Posture.service"
            )
            if self.require(activation is not None, "D-Bus activation file is missing"):
                assert activation is not None
                parser = configparser.ConfigParser(interpolation=None)
                parser.optionxform = str
                try:
                    parser.read_string(activation.read_text(encoding="utf-8"))
                    service = parser["D-BUS Service"]
                    self.require(service.get("Name") == INTERFACE, "D-Bus activation name is wrong")
                    self.require(
                        service.get("SystemdService")
                        == "panasonic-posture-manager.service",
                        "D-Bus activation points at the wrong systemd unit",
                    )
                except (configparser.Error, KeyError) as error:
                    self.fail(f"invalid D-Bus activation file: {error}")

            modules_load = self.files.get(
                "usr/lib/modules-load.d/panasonic-posture-manager.conf"
            )
            if modules_load is not None:
                self.require(
                    "uinput" in modules_load.read_text(encoding="utf-8").split(),
                    "modules-load configuration does not request uinput",
                )

            desktop = self.files.get(
                "usr/share/applications/work.mmoe.panasonic.PostureManager.desktop"
            )
            if self.require(desktop is not None, "desktop entry is missing"):
                assert desktop is not None
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                parser.optionxform = str
                try:
                    parser.read_string(desktop.read_text(encoding="utf-8"))
                    entry = parser["Desktop Entry"]
                    self.require(
                        entry.get("Exec") == "/usr/bin/panasonic-posture-manager",
                        "desktop entry Exec path is wrong",
                    )
                    icon = entry.get("Icon")
                    self.require(bool(icon), "desktop entry has no icon")
                    if icon:
                        self.require(
                            f"usr/share/icons/hicolor/512x512/apps/{icon}.png" in self.files,
                            f"desktop icon is not packaged: {icon}.png",
                        )
                except (configparser.Error, KeyError) as error:
                    self.fail(f"invalid desktop entry: {error}")
                if shutil.which("desktop-file-validate"):
                    result = self.run_checked(["desktop-file-validate", str(desktop)])
                    if result is not None:
                        self.require(
                            result.returncode == 0,
                            f"desktop-file-validate rejected the entry: {result.stdout.strip()}",
                        )
                else:
                    self.warn("desktop-file-validate is unavailable; entry was parsed manually")

            install_script = self.extract_root / ".INSTALL" if self.extract_root else None
            if install_script is not None and install_script.is_file():
                text = install_script.read_text(encoding="utf-8")
                for hook in ("post_install", "post_upgrade", "pre_remove", "post_remove"):
                    self.require(f"{hook}()" in text, f"install script is missing {hook}()")
                if shutil.which("bash"):
                    result = self.run_checked(["bash", "-n", str(install_script)])
                    if result is not None:
                        self.require(
                            result.returncode == 0,
                            f"install script has a shell syntax error: {result.stdout.strip()}",
                        )

    def check_runtime(self) -> None:
        with self.stage("installed Python and probe entry points"):
            site = self.extract_root / self.site_dir if self.extract_root else None
            ui_dir = (
                self.extract_root / "usr/share/panasonic-posture-manager/assets"
                if self.extract_root
                else None
            )
            if site is None or ui_dir is None:
                self.fail("extracted package root is unavailable")
                return
            env = os.environ.copy()
            env["PYTHONPATH"] = str(site)
            env["PANASONIC_POSTURE_UI_DIR"] = str(ui_dir.parent / "ui")
            code = (
                "import panasonic_posture\n"
                "from panasonic_posture import protocol\n"
                "from panasonic_posture.ui.common import DATA_ROOT\n"
                f"assert panasonic_posture.__version__ == {self.version!r}\n"
                f"assert protocol.NAME == {INTERFACE!r}\n"
                "assert (DATA_ROOT / 'assets/laptop.svg').is_file()\n"
            )
            result = self.run_checked([sys.executable, "-c", code], env=env)
            if result is not None:
                self.require(
                    result.returncode == 0,
                    f"packaged Python modules cannot be imported: {result.stdout.strip()}",
                )

            probe_script = self.extract_root / "usr/bin/panasonic-posture-manager-probe"
            if probe_script.is_file():
                result = self.run_checked([sys.executable, str(probe_script), "--help"], env=env)
                if result is not None:
                    self.require(
                        result.returncode == 0,
                        f"packaged probe entry point failed: {result.stdout.strip()}",
                    )
                    self.require(
                        "usage: panasonic-posture-manager-probe" in result.stdout,
                        "packaged probe entry point returned unexpected output",
                    )

    def package_regular_files(self, archive: Path) -> set[str]:
        return {
            entry.rstrip("/")
            for entry in self.archive_entries(archive)
            if not entry.endswith("/") and entry.rstrip("/") not in META_FILES
        }

    def pacman_prefix(self) -> list[str] | None:
        if os.geteuid() == 0:
            return []
        unshare = shutil.which("unshare")
        if unshare is None:
            self.fail(
                "installation checks need root or unshare; run as root or use --no-install"
            )
            return None
        result = self.run_checked([unshare, "--map-root-user", "true"])
        if result is None or result.returncode != 0:
            detail = result.stdout.strip() if result is not None else "unshare failed"
            self.fail(
                "installation checks need root or an available user namespace; "
                f"run as root or use --no-install ({detail})"
            )
            return None
        return [unshare, "--map-root-user"]

    def check_installation(self) -> None:
        if self.previous is not None:
            previous_info = parse_key_values(self.archive_file(self.previous, ".PKGINFO"))
            self.require(
                one_value(previous_info, "pkgname", self) == PACKAGE_NAME,
                "previous package has a different package name",
            )

        prefix = self.pacman_prefix()
        if prefix is None:
            return
        assert self.temp_root is not None
        install_root = self.temp_root / "pacman-root"
        cache_dir = install_root / "var/cache/pacman/pkg"
        db_dir = install_root / "var/lib/pacman"
        log_file = install_root / "var/log/pacman.log"
        for directory in (cache_dir, db_dir / "local", log_file.parent, install_root / "usr", install_root / "etc"):
            directory.mkdir(parents=True, exist_ok=True)
        config = self.temp_root / "pacman.conf"
        config.write_text(
            "[options]\nArchitecture = auto\nSigLevel = Never\nLocalFileSigLevel = Never\n",
            encoding="utf-8",
        )

        def pacman(*arguments: str) -> bool:
            command = prefix + [
                "pacman",
                "--config",
                str(config),
                "--root",
                str(install_root),
                "--dbpath",
                str(db_dir),
                "--cachedir",
                str(cache_dir),
                "--logfile",
                str(log_file),
                *arguments,
            ]
            result = self.run_checked(command)
            if result is None:
                return False
            if result.returncode != 0:
                self.fail(f"pacman {' '.join(arguments)} failed:\n{result.stdout.strip()}")
                return False
            return True

        with self.stage("fresh install and isolated pacman transaction"):
            if self.previous is not None:
                if not pacman("-Udd", "--noconfirm", "--noscriptlet", str(self.previous)):
                    return
                self.require(
                    bool(list((db_dir / "local").glob(f"{PACKAGE_NAME}-*"))),
                    "previous package was not registered in the isolated package database",
                )
            if not pacman("-Udd", "--noconfirm", "--noscriptlet", str(self.package)):
                return
            self.check_installed_tree(install_root, db_dir)

            if self.previous is None:
                if not pacman("-Udd", "--noconfirm", "--noscriptlet", str(self.package)):
                    return
            else:
                old_files = self.package_regular_files(self.previous)
                new_files = self.package_regular_files(self.package)
                for obsolete in sorted(old_files - new_files):
                    self.require(
                        not (install_root / obsolete).exists(),
                        f"upgrade left an obsolete file behind: {obsolete}",
                    )

        with self.stage("package removal"):
            if not pacman("-Rdd", "--noconfirm", "--noscriptlet", PACKAGE_NAME):
                return
            for relative in sorted(self.package_regular_files(self.package)):
                self.require(
                    not (install_root / relative).exists(),
                    f"removal left a package file behind: {relative}",
                )
            self.require(
                not list((db_dir / "local").glob(f"{PACKAGE_NAME}-*")),
                "removal left the package registered in the isolated database",
            )

    def check_installed_tree(self, install_root: Path, db_dir: Path) -> None:
        self.require(
            bool(list((db_dir / "local").glob(f"{PACKAGE_NAME}-{self.pkgver}-{self.pkgrel}"))),
            "package was not registered in the isolated package database",
        )
        for relative, extracted in self.files.items():
            installed = install_root / relative
            if not self.require(installed.is_file(), f"install did not create: {relative}"):
                continue
            self.require(
                installed.read_bytes() == extracted.read_bytes(),
                f"installed file differs from package archive: {relative}",
            )
            self.require(
                stat.S_IMODE(installed.stat().st_mode)
                == stat.S_IMODE(extracted.stat().st_mode),
                f"installed mode differs from package archive: {relative}",
            )

    def run(self) -> int:
        with tempfile.TemporaryDirectory(prefix="ppm-package-acceptance-") as temporary:
            self.temp_root = Path(temporary)
            self.check_archive()
            if not self.errors:
                self.check_metadata()
            if not self.errors:
                self.check_layout()
            if not self.errors:
                self.check_modes()
            if not self.errors:
                self.check_service_configuration()
            if not self.errors:
                self.check_runtime()
            if self.run_install and not self.errors:
                self.check_installation()

        print()
        if self.errors:
            print(
                f"Package acceptance FAILED: {len(self.errors)} error(s), "
                f"{len(self.warnings)} warning(s).",
                file=sys.stderr,
            )
            return 1
        print(
            f"Package acceptance passed: {self.passes} check group(s), "
            f"{len(self.warnings)} warning(s)."
        )
        if not self.run_install:
            print("Install/upgrade/remove checks were skipped with --no-install.")
        return 0


def parse_key_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition(" = ")
        if separator:
            values.setdefault(key, []).append(value)
    return values


def parse_srcinfo(text: str) -> dict[str, list[tuple[str, dict[str, list[str]]]]]:
    sections: dict[str, list[tuple[str, dict[str, list[str]]]]] = {
        "pkgbase": [],
        "pkgname": [],
    }
    current: dict[str, list[str]] | None = None
    for line in text.splitlines():
        if not line:
            continue
        if line.startswith((" ", "\t")):
            if current is None:
                continue
            key, separator, value = line.strip().partition(" = ")
            if separator:
                current.setdefault(key, []).append(value)
            continue
        key, separator, value = line.partition(" = ")
        if separator and key in sections:
            current = {}
            sections[key].append((value, current))
        else:
            current = None
    return sections


def first_srcinfo_section(
    sections: dict[str, list[tuple[str, dict[str, list[str]]]]], kind: str
) -> dict[str, list[str]] | None:
    entries = sections.get(kind, [])
    return entries[0][1] if entries else None


def section_by_name(
    sections: dict[str, list[tuple[str, dict[str, list[str]]]]],
    kind: str,
    name: str,
) -> dict[str, list[str]] | None:
    for section_name, values in sections.get(kind, []):
        if section_name == name:
            return values
    return None


def one_value(values: dict[str, list[str]], key: str, checker: PackageAcceptance) -> str:
    entries = values.get(key, [])
    if len(entries) != 1:
        checker.fail(f"expected exactly one {key} value, found {len(entries)}")
        return ""
    return entries[0]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path, help="built .pkg.tar.zst")
    parser.add_argument(
        "--previous",
        type=Path,
        help="optional previous package; when supplied, the check performs an upgrade",
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        default=REPO_ROOT,
        help="repository checkout used to compare package contents (default: repository root)",
    )
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="skip the isolated pacman install/reinstall/remove transaction",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    checker = PackageAcceptance(
        arguments.package,
        source_root=arguments.source_root,
        previous=arguments.previous,
        install=not arguments.no_install,
    )
    print(f"Package: {checker.package}")
    print(f"Source:  {checker.source_root}")
    if checker.previous is not None:
        print(f"Previous: {checker.previous}")
    print()
    try:
        return checker.run()
    except KeyboardInterrupt:
        print("Package acceptance interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
