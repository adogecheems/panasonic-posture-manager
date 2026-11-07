"""Contracts for the Debian, RPM and release-workflow packaging path."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = ROOT / "packaging/scripts/release_version.py"
PACKAGE_SCRIPTS = (
    "packaging/scripts/stage-install-tree.sh",
    "packaging/scripts/build-deb.sh",
    "packaging/scripts/build-rpm.sh",
    "packaging/scripts/generate-aur-bin.sh",
    "packaging/scripts/publish-aur.sh",
)
AUR_BIN_GENERATOR = ROOT / "packaging/scripts/generate-aur-bin.sh"
AUR_BIN_CHECK = ROOT / "tests/package/check_aur_bin.py"


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

    def test_aur_bin_checker_parses(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(AUR_BIN_CHECK)],
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
            "python3-gi-cairo",
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
            "Requires:       python3-cairo",
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

    def test_aur_bin_template_matches_source_dependencies(self) -> None:
        source = read("packaging/aur/.SRCINFO")
        template = read("packaging/aur-bin/PKGBUILD.in")
        for field in ("depends", "optdepends"):
            for line in source.splitlines():
                prefix = f"\t{field} = "
                if line.startswith(prefix):
                    with self.subTest(field=field, value=line):
                        self.assertIn(f"  '{line.removeprefix(prefix)}'", template)


class AurBinGeneratorTests(unittest.TestCase):
    def test_generator_writes_versioned_pkgbuild_and_checksum(self) -> None:
        version = read("pyproject.toml").split('version = "', 1)[1].split('"', 1)[0]
        payload = b"verified arch package"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / f"panasonic-posture-manager-{version}-1-x86_64.pkg.tar.zst"
            package.write_bytes(payload)
            output = root / "metadata"
            result = subprocess.run(
                [str(AUR_BIN_GENERATOR), f"v{version}", str(package), str(output)],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            pkgbuild = (output / "PKGBUILD").read_text(encoding="utf-8")
            self.assertIn("pkgname=panasonic-posture-manager-bin", pkgbuild)
            self.assertIn(f"pkgver={version}", pkgbuild)
            self.assertIn(hashlib.sha256(payload).hexdigest(), pkgbuild)
            self.assertIn("provides=('panasonic-posture-manager')", pkgbuild)
            self.assertIn("conflicts=('panasonic-posture-manager')", pkgbuild)
            self.assertTrue((output / "panasonic-posture-manager-bin.install").is_file())

            if shutil.which("makepkg"):
                result = subprocess.run(
                    ["makepkg", "--printsrcinfo"],
                    cwd=output,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("pkgbase = panasonic-posture-manager-bin", result.stdout)
                self.assertIn(f"pkgver = {version}", result.stdout)

    def test_generator_rejects_a_mismatched_tag(self) -> None:
        version = read("pyproject.toml").split('version = "', 1)[1].split('"', 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / f"panasonic-posture-manager-{version}-1-x86_64.pkg.tar.zst"
            package.write_bytes(b"package")
            result = subprocess.run(
                [str(AUR_BIN_GENERATOR), "v0.0.0", str(package), str(root / "metadata")],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("does not match release", result.stderr)


class ReleaseWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow = read(".github/workflows/release.yml")

    def test_tag_push_and_manual_rebuild_are_supported(self) -> None:
        self.assertIn("  push:\n    tags:\n      - 'v[0-9]*'", self.workflow)
        self.assertIn("  workflow_dispatch:", self.workflow)
        self.assertIn("RELEASE_TAG: ${{ inputs.tag || github.ref_name }}", self.workflow)
        self.assertIn(
            "SOURCE_REF: ${{ github.event_name == 'workflow_dispatch' && "
            "github.sha || github.ref_name }}",
            self.workflow,
        )
        self.assertEqual(self.workflow.count("ref: ${{ env.SOURCE_REF }}"), 4)

    def test_arch_build_uses_checkout_and_exports_tag_pinned_aur_metadata(self) -> None:
        arch = self.workflow.split("  package-arch:", 1)[1].split("  package-deb:", 1)[0]
        archive = "git archive --format=tar.gz --prefix=panasonic-posture-manager/"
        override = "source[0]=ppm-release-source.tar.gz"
        build = "makepkg --cleanbuild --noconfirm --nodeps -p PKGBUILD.ci"
        self.assertIn(archive, arch)
        self.assertIn("-o packaging/aur/ppm-release-source.tar.gz HEAD", arch)
        self.assertIn(override, arch)
        self.assertIn(build, arch)
        self.assertLess(arch.index(archive), arch.index(override))
        self.assertLess(arch.index(override), arch.index(build))
        self.assertIn("makepkg --printsrcinfo", arch)
        self.assertIn("/tmp/ppm-arch/packaging/aur/PKGBUILD /work/dist/aur/PKGBUILD", arch)
        self.assertIn("#tag=v${pkgver}", read("packaging/aur/PKGBUILD"))

    def test_all_three_package_builders_are_present(self) -> None:
        for job in ("package-arch:", "package-deb:", "package-rpm:"):
            with self.subTest(job=job):
                self.assertIn(job, self.workflow)
        for artifact in ("arch-package", "deb-package", "rpm-package"):
            with self.subTest(artifact=artifact):
                self.assertIn(f"name: {artifact}", self.workflow)
        arch = self.workflow.split("  package-arch:", 1)[1].split("  package-deb:", 1)[0]
        self.assertIn('su - builder -c "cd /tmp/ppm-arch && make WERROR=1 check"', arch)
        self.assertIn('! -name "*-debug-*"', arch)
        self.assertIn(
            'make check-package PACKAGE="/work/dist/release/$(basename "$package")"',
            arch,
        )
        rpm = self.workflow.split("  package-rpm:", 1)[1].split("  release:", 1)[0]
        self.assertIn('rpm_files=$(rpm -qlp "$package")', rpm)
        self.assertNotIn('rpm -qlp "$package" | grep', rpm)

    def test_arch_job_installs_python_cairo_for_ui_checks(self) -> None:
        arch = self.workflow.split("  package-arch:", 1)[1].split("  package-deb:", 1)[0]
        install = arch.split("pacman -Syu --noconfirm --needed", 1)[1].split(
            "useradd -m builder", 1
        )[0]
        for dependency in ("python-cairo", "python-gobject"):
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, install)

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
        self.assertIn("name: aur-bin-metadata", aur)
        self.assertIn("AUR_PACKAGE: panasonic-posture-manager-bin", aur)
        self.assertIn("packaging/scripts/publish-aur.sh aur-bin-metadata", aur)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", self.workflow)

    def test_arch_job_builds_and_checks_the_aur_bin_package(self) -> None:
        arch = self.workflow.split("  package-arch:", 1)[1].split("  package-deb:", 1)[0]
        self.assertIn("name: aur-bin-metadata", self.workflow)
        self.assertIn("generate-aur-bin.sh", arch)
        self.assertIn("check_aur_bin.py", arch)
        self.assertIn("SRCDEST=/tmp/ppm-arch/packaging/aur-bin/srcdest", arch)
        self.assertIn("panasonic-posture-manager-bin-[0-9]*.pkg.tar.zst", arch)
        self.assertIn("makepkg --printsrcinfo > SRCINFO", arch)
        self.assertLess(
            arch.index("makepkg --printsrcinfo > SRCINFO"),
            arch.index("check_aur_bin.py"),
        )
        self.assertEqual(arch.count("'"), 2)

    def test_aur_host_key_is_pinned(self) -> None:
        known_hosts = read("packaging/ci/aur_known_hosts")
        self.assertEqual(len(known_hosts.strip().splitlines()), 1)
        self.assertRegex(
            known_hosts,
            r"^aur\.archlinux\.org ssh-ed25519 [A-Za-z0-9+/=]+$",
        )


if __name__ == "__main__":
    unittest.main()
