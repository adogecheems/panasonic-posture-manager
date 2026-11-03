"""Contracts for the Debian, RPM and release-workflow packaging path."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = ROOT / "packaging/scripts/release_version.py"
PACKAGE_SCRIPTS = (
    "packaging/scripts/stage-install-tree.sh",
    "packaging/scripts/build-deb.sh",
    "packaging/scripts/build-rpm.sh",
    "packaging/scripts/publish-aur.sh",
)


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class ReleaseVersionTests(unittest.TestCase):
    def run_version(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RELEASE_VERSION), *arguments],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_current_tag_and_metadata_agree(self) -> None:
        version = read("pyproject.toml").split('version = "', 1)[1].split('"', 1)[0]
        result = self.run_version("--tag", f"v{version}")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), version)

    def test_mismatched_tag_is_rejected(self) -> None:
        result = self.run_version("--tag", "v0.0.0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match release", result.stderr)


class DistributionPackagingTests(unittest.TestCase):
    def test_package_scripts_are_executable_and_parse(self) -> None:
        for relative in PACKAGE_SCRIPTS:
            with self.subTest(relative=relative):
                path = ROOT / relative
                self.assertTrue(os.access(path, os.X_OK), f"{relative} is not executable")
                result = subprocess.run(
                    ["bash", "-n", str(path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_release_helper_parses(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(RELEASE_VERSION)],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deb_control_declares_wayland_runtime(self) -> None:
        control = read("packaging/deb/control.in")
        for dependency in (
            "dbus",
            "gir1.2-adw-1",
            "gir1.2-gtk-4.0",
            "gjs",
            "iio-sensor-proxy",
            "libadwaita-1-0",
            "libgtk-4-1",
            "polkitd",
            "python3",
            "python3-gi",
            "systemd",
        ):
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, control)
        for placeholder in ("@VERSION@", "@ARCH@", "@INSTALLED_SIZE@"):
            self.assertIn(placeholder, control)

    def test_deb_maintainer_scripts_parse(self) -> None:
        for name in ("postinst", "prerm", "postrm"):
            with self.subTest(name=name):
                path = ROOT / "packaging/deb" / name
                result = subprocess.run(
                    ["sh", "-n", str(path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rpm_spec_and_scriptlets_match_install_layout(self) -> None:
        spec = read("packaging/rpm/panasonic-posture-manager.spec.in")
        for content in (
            "Requires:       gjs",
            "Requires:       gtk4",
            "Requires:       libadwaita",
            "Requires:       python3-gobject",
            "Requires:       systemd",
            "/usr/bin/panasonic-posture-manager",
            "/usr/lib/panasonic-posture-manager/relay",
            "/usr/lib/systemd/system/panasonic-posture-manager.service",
            "/usr/lib/systemd/user/panasonic-posture-manager-session.service",
            "%license /usr/share/licenses/panasonic-posture-manager/LICENSE",
            "%{python3_sitelib}/panasonic_posture",
            "stage-install-tree.sh",
        ):
            with self.subTest(content=content):
                self.assertIn(content, spec)
        self.assertIn("@VERSION@", spec)
        self.assertIn("%post", spec)
        self.assertIn("%preun", spec)
        self.assertIn("systemctl try-restart panasonic-posture-manager.service", spec)

    def test_shared_tree_uses_python3_and_the_expected_paths(self) -> None:
        stage = read("packaging/scripts/stage-install-tree.sh")
        self.assertIn("--prefix /usr", stage)
        self.assertIn("#!/usr/bin/python3", stage)
        self.assertIn("DEB_PYTHON_INSTALL_LAYOUT=deb_system", stage)
        self.assertIn("usr/lib/panasonic-posture-manager/relay", stage)
        self.assertIn("usr/share/licenses/panasonic-posture-manager/LICENSE", stage)
        for unit in (
            "packaging/systemd/panasonic-posture-manager.service",
            "packaging/systemd/user/panasonic-posture-manager-session.service",
        ):
            self.assertIn("ExecStart=/usr/bin/python3", read(unit))


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = read(".github/workflows/release.yml")

    def test_tag_push_and_manual_rebuild_are_supported(self) -> None:
        self.assertIn("  push:\n    tags:\n      - 'v[0-9]*'", self.workflow)
        self.assertIn("  workflow_dispatch:", self.workflow)
        self.assertIn("RELEASE_TAG: ${{ inputs.tag || github.ref_name }}", self.workflow)

    def test_all_three_package_builders_are_present(self) -> None:
        for job in ("package-arch:", "package-deb:", "package-rpm:"):
            with self.subTest(job=job):
                self.assertIn(job, self.workflow)
        for artifact in ("arch-package", "deb-package", "rpm-package"):
            with self.subTest(artifact=artifact):
                self.assertIn(f"name: {artifact}", self.workflow)
        arch = self.workflow.split("  package-arch:", 1)[1].split("  package-deb:", 1)[0]
        self.assertIn('su - builder -c "cd /tmp/ppm-arch && make WERROR=1 check"', arch)
        self.assertIn(
            'su - builder -c "cd /tmp/ppm-arch/packaging/aur && '
            'makepkg --cleanbuild --noconfirm --nodeps"',
            arch,
        )
        rpm = self.workflow.split("  package-rpm:", 1)[1].split("  release:", 1)[0]
        self.assertIn('rpm_files=$(rpm -qlp "$package")', rpm)
        self.assertNotIn('rpm -qlp "$package" | grep', rpm)

    def test_release_waits_for_all_packages_and_writes_assets(self) -> None:
        release = self.workflow.split("  release:", 1)[1].split("  aur:", 1)[0]
        for dependency in ("package-arch", "package-deb", "package-rpm"):
            self.assertIn(f"      - {dependency}", release)
        self.assertIn("contents: write", release)
        self.assertIn("gh release upload", release)
        self.assertIn("gh release create", release)
        self.assertIn("SHA256SUMS", release)

    def test_aur_push_uses_generated_metadata_and_a_secret(self) -> None:
        aur = self.workflow.split("  aur:", 1)[1]
        self.assertIn("needs: release", aur)
        self.assertIn("name: aur-metadata", aur)
        self.assertIn("secrets.AUR_SSH_PRIVATE_KEY", aur)
        self.assertIn("packaging/scripts/publish-aur.sh aur-metadata", aur)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", self.workflow)

    def test_aur_host_key_is_pinned(self) -> None:
        known_hosts = read("packaging/ci/aur_known_hosts")
        self.assertEqual(len(known_hosts.strip().splitlines()), 1)
        self.assertRegex(
            known_hosts,
            r"^aur\.archlinux\.org ssh-ed25519 [A-Za-z0-9+/=]+$",
        )


if __name__ == "__main__":
    unittest.main()
