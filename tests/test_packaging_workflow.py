"""Contracts for the Debian, RPM and release-workflow packaging path."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_VERSION = ROOT / "packaging/common/release_version.py"
PACKAGE_SCRIPTS = (
    "packaging/linux/scripts/stage-install-tree.sh",
    "packaging/linux/scripts/build-deb.sh",
    "packaging/linux/scripts/build-rpm.sh",
    "packaging/linux/scripts/generate-aur-bin.sh",
    "packaging/linux/scripts/publish-aur.sh",
)
AUR_BIN_GENERATOR = ROOT / "packaging/linux/scripts/generate-aur-bin.sh"
AUR_BIN_CHECK = ROOT / "tests/package/linux/check_aur_bin.py"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class ReleaseVersionTests(unittest.TestCase):
    def test_exported_aur_source_pins_the_build_commit_and_validates_inputs(self):
        spec = importlib.util.spec_from_file_location(
            'pin_aur_source', ROOT / 'packaging/linux/scripts/pin_aur_source.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        recipe = read('packaging/linux/aur/PKGBUILD')
        commit = 'a' * 40
        pinned = module.pin_source(recipe, commit)
        self.assertIn(f'#commit={commit}', pinned)
        self.assertNotIn('#tag=v${pkgver}', pinned)
        self.assertEqual(pinned.replace(f'#commit={commit}', '#tag=v${pkgver}'), recipe)
        for invalid in ('main', 'v2.5.0', '', 'a' * 39):
            with self.subTest(commit=invalid), self.assertRaises(ValueError):
                module.pin_source(recipe, invalid)
        with self.assertRaises(ValueError):
            module.pin_source('no source declaration', commit)

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
    def test_staged_linux_assets_match_shared_sources(self):
        bash = shutil.which('bash')
        if bash is None and sys.platform == 'win32':
            candidate = Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Git/bin/bash.exe'
            bash = str(candidate) if candidate.is_file() else None
        if bash is None:
            self.skipTest('Bash is unavailable')
        # Exercise the real staging script. Only the wheel installer and its
        # prerequisite build outputs are fixtures; no package is installed.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = Path('packaging/linux/scripts/stage-install-tree.sh')
            (root / script).parent.mkdir(parents=True)
            shutil.copyfile(ROOT / script, root / script)
            shutil.copytree(ROOT / 'data', root / 'data')
            shutil.copytree(ROOT / 'packaging/linux', root / 'packaging/linux', dirs_exist_ok=True)
            for name in ('backend/linux/relay', 'locale/zh_CN/LC_MESSAGES/panasonic-posture-manager.mo',
                         'fixture.whl', 'LICENSE'):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('#!/usr/bin/env bash\nexit 0\n', encoding='utf-8', newline='\n')
                path.chmod(0o755)
            installer = root / 'fixture-bin/python3'
            installer.parent.mkdir()
            installer.write_text('''#!/usr/bin/env bash
if [[ $1 == -c ]]; then exit 1; fi
while [[ $# -gt 0 ]]; do
  if [[ $1 == --destdir ]]; then shift; destdir=$1; fi
  shift
done
mkdir -p "$destdir/usr/bin"
for name in panasonic-posture-manager panasonic-posture-manager-probe; do
  printf '#!/usr/bin/python3\\n' > "$destdir/usr/bin/$name"
done
''', encoding='utf-8', newline='\n')
            installer.chmod(0o755)
            command = 'PATH="$PWD/fixture-bin:$PATH" bash packaging/linux/scripts/stage-install-tree.sh stage fixture.whl'
            result = subprocess.run([bash, '-c', command], cwd=root, text=True,
                                    capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            assets = root / 'stage/usr/share/panasonic-posture-manager/assets'
            sources = ROOT / 'data/assets/common'
            expected = {path.relative_to(sources) for path in sources.rglob('*') if path.is_file()}
            self.assertEqual({path.relative_to(assets) for path in assets.rglob('*') if path.is_file()}, expected)
            for name in expected:
                self.assertEqual((assets / name).read_bytes(), (sources / name).read_bytes())
            self.assertEqual(
                (root / 'stage/usr/share/icons/hicolor/512x512/apps/work.mmoe.panasonic.PostureManager.png').read_bytes(),
                (sources / 'icons/work.mmoe.panasonic.PostureManager.png').read_bytes(),
            )

    def test_arch_payload_maps_linux_resources_to_the_runtime_asset_directory(self):
        spec = importlib.util.spec_from_file_location(
            'check_arch_payload', ROOT / 'tests/package/linux/check_package.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        checker = module.PackageAcceptance(ROOT / 'unused.pkg.tar.zst',
            source_root=ROOT, previous=None, install=False)
        copies = checker.expected_source_copies()
        self.assertEqual(copies['usr/share/panasonic-posture-manager/assets/postures/laptop.svg'],
                         ROOT / 'data/assets/common/postures/laptop.svg')
        self.assertEqual(copies['usr/share/panasonic-posture-manager/profiles/cfqv8.json'],
                         ROOT / 'data/profiles/cfqv8.json')
        self.assertFalse(any('/assets/windows/' in str(path) for path in copies.values()))
        self.assertFalse(any('/assets/linux/' in name for name in copies))

    def test_package_scripts_are_executable_and_parse(self) -> None:
        if sys.platform == "win32":
            self.skipTest("POSIX package scripts require a Linux build host")
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
        control = read("packaging/linux/deb/control.in")
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
        shell = shutil.which("sh")
        if shell is None:
            self.skipTest("POSIX sh is unavailable")
        for name in ("postinst", "prerm", "postrm"):
            with self.subTest(name=name):
                path = ROOT / "packaging/linux/deb" / name
                result = subprocess.run(
                    [shell, "-n", str(path)],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_rpm_spec_and_scriptlets_match_install_layout(self) -> None:
        spec = read("packaging/linux/rpm/panasonic-posture-manager.spec.in")
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
        stage = read("packaging/linux/scripts/stage-install-tree.sh")
        self.assertIn("--prefix /usr", stage)
        self.assertIn("#!/usr/bin/python3", stage)
        self.assertIn("DEB_PYTHON_INSTALL_LAYOUT=deb_system", stage)
        self.assertIn("usr/lib/panasonic-posture-manager/relay", stage)
        self.assertIn("usr/share/licenses/panasonic-posture-manager/LICENSE", stage)
        for unit in (
            "packaging/linux/systemd/panasonic-posture-manager.service",
            "packaging/linux/systemd/user/panasonic-posture-manager-session.service",
        ):
            content = read(unit)
            self.assertIn("ExecStart=/usr/bin/python3", content)
            self.assertIn(
                "Description=Panasonic 2-in-1 Tablet Posture Management Tool",
                content,
            )

    def test_aur_bin_template_matches_source_dependencies(self) -> None:
        source = read("packaging/linux/aur/.SRCINFO")
        template = read("packaging/linux/aur-bin/PKGBUILD.in")
        for field in ("depends", "optdepends"):
            for line in source.splitlines():
                prefix = f"\t{field} = "
                if line.startswith(prefix):
                    with self.subTest(field=field, value=line):
                        self.assertIn(f"  '{line.removeprefix(prefix)}'", template)


@unittest.skipUnless(sys.platform == "linux", "AUR packaging requires Linux")
class AurBinGeneratorTests(unittest.TestCase):
    def test_generator_writes_versioned_pkgbuild_and_checksum(self) -> None:
        version = read("pyproject.toml").split('version = "', 1)[1].split('"', 1)[0]
        pkgrel = re.search(
            r"^pkgrel=(\S+)$", read("packaging/linux/aur/PKGBUILD"), re.MULTILINE
        ).group(1)
        payload = b"verified arch package"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / f"panasonic-posture-manager-{version}-{pkgrel}-x86_64.pkg.tar.zst"
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
            self.assertIn(f"pkgrel={pkgrel}", pkgbuild)
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
        pkgrel = re.search(
            r"^pkgrel=(\S+)$", read("packaging/linux/aur/PKGBUILD"), re.MULTILINE
        ).group(1)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / f"panasonic-posture-manager-{version}-{pkgrel}-x86_64.pkg.tar.zst"
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

    def test_daily_workflows_do_not_filter_out_metadata_or_documentation(self):
        for platform in ('linux', 'windows'):
            with self.subTest(platform=platform):
                workflow = read(f'.github/workflows/{platform}.yml')
                triggers = workflow.split('permissions:', 1)[0]
                self.assertIn('  push:\n', triggers)
                self.assertIn('  pull_request:\n', triggers)
                self.assertIn('  workflow_dispatch:\n', triggers)
                self.assertNotIn('paths:', triggers)
                self.assertNotIn('paths-ignore:', triggers)

    def test_linux_matrix_make_steps_use_the_selected_python(self):
        workflow = read('.github/workflows/linux.yml')
        matrix = workflow.split('  linux:', 1)[1].split('  linux-full:', 1)[0]
        self.assertIn('python-version: ${{ matrix.python }}', matrix)
        commands = re.findall(r'run: (make .*)', matrix)
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertIn('PYTHON=python', command)

    def test_linux_daily_ci_runs_full_checks_with_native_dependencies(self):
        workflow = read('.github/workflows/linux.yml')
        self.assertIn('  workflow_call:\n', workflow)
        full = workflow.split('  linux-full:', 1)[1]
        self.assertNotIn('actions/setup-python', full)
        for dependency in ('dbus', 'python3-gi', 'python3-gi-cairo',
                           'gir1.2-gtk-4.0', 'gir1.2-adw-1', 'libgtk-4-bin',
                           'desktop-file-utils', 'libxml2-utils'):
            with self.subTest(dependency=dependency):
                self.assertIn(dependency, full)
        self.assertIn('command -v gtk4-broadwayd', full)
        self.assertIn('make WERROR=1 PYTHON=/usr/bin/python3 check', full)

    def test_release_requires_linux_checks_from_its_own_source(self):
        checks = self.workflow.split('  check-linux:', 1)[1].split('  package-arch:', 1)[0]
        self.assertIn('uses: ./.github/workflows/linux.yml', checks)
        release = self.workflow.split('  release:', 1)[1].split('  aur:', 1)[0]
        self.assertIn('      - check-linux\n', release)

    def test_linux_workflow_references_existing_test_modules(self) -> None:
        modules = re.findall(r'tests\.(test_\w+)', read('.github/workflows/linux.yml'))
        self.assertTrue(modules)
        for module in modules:
            with self.subTest(module=module):
                self.assertTrue((ROOT / 'tests' / f'{module}.py').is_file())

    def test_tag_push_and_manual_rebuild_are_supported(self) -> None:
        self.assertIn("  push:\n    tags:\n      - 'v[0-9]*'", self.workflow)
        self.assertIn("  workflow_dispatch:", self.workflow)
        self.assertIn("RELEASE_TAG: ${{ inputs.tag || github.ref_name }}", self.workflow)
        self.assertIn(
            "SOURCE_REF: ${{ github.event_name == 'workflow_dispatch' && "
            "github.sha || github.ref_name }}",
            self.workflow,
        )
        self.assertEqual(self.workflow.count("ref: ${{ env.SOURCE_REF }}"), 6)

    def test_arch_build_uses_checkout_and_exports_tag_pinned_aur_metadata(self) -> None:
        arch = self.workflow.split("  package-arch:", 1)[1].split("  package-deb:", 1)[0]
        archive = "git archive --format=tar.gz --prefix=panasonic-posture-manager/"
        override = "source[0]=ppm-release-source.tar.gz"
        build = "makepkg --cleanbuild --noconfirm --nodeps -p PKGBUILD.ci"
        self.assertIn(archive, arch)
        self.assertIn("-o packaging/linux/aur/ppm-release-source.tar.gz HEAD", arch)
        self.assertIn(override, arch)
        self.assertIn(build, arch)
        self.assertLess(arch.index(archive), arch.index(override))
        self.assertLess(arch.index(override), arch.index(build))
        self.assertIn("makepkg --printsrcinfo", arch)
        self.assertIn("/tmp/ppm-arch/packaging/linux/aur/PKGBUILD.release /work/dist/aur/PKGBUILD", arch)
        self.assertIn('makepkg --printsrcinfo -p PKGBUILD.release', arch)
        self.assertIn('pin_aur_source.py packaging/linux/aur/PKGBUILD.release', arch)
        self.assertIn("#tag=v${pkgver}", read("packaging/linux/aur/PKGBUILD"))

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

    def test_windows_msi_is_a_release_package(self) -> None:
        windows = self.workflow.split("  package-windows:", 1)[1].split("  release:", 1)[0]
        self.assertIn("runs-on: windows-latest", windows)
        self.assertIn("dotnet tool install --global wix --version 5.0.2", windows)
        self.assertIn("WixToolset.UI.wixext/5.0.2", windows)
        self.assertIn("python packaging/windows/scripts/build_msi.py", windows)
        self.assertIn("--culture all", windows)
        self.assertIn("name: windows-package", windows)
        self.assertIn("dist/windows/*.msi", windows)
        self.assertIn("files=(*.deb *.rpm *.pkg.tar.zst *.msi)", self.workflow)
        self.assertIn("expected five package files", self.workflow)

    def test_windows_ci_does_not_treat_portable_zip_as_contract(self) -> None:
        windows = read(".github/workflows/windows.yml")
        self.assertNotIn(
            "python packaging/windows/scripts/build.py --dependencies",
            windows,
        )
        self.assertNotIn(
            "panasonic-posture-windows-portable.zip",
            windows,
        )
        self.assertNotIn("windows_bundle_smoke.py", windows)
        self.assertIn("path: dist/windows/*.msi", windows)

    def test_windows_workflows_cache_downloads_and_skip_discarded_bytecode(self) -> None:
        for workflow in (".github/workflows/windows.yml", ".github/workflows/release.yml"):
            source = read(workflow)
            with self.subTest(workflow=workflow):
                self.assertIn("cache: pip", source)
                self.assertIn("cache-dependency-path: packaging/windows/dependencies/requirements-ui.txt", source)
                self.assertIn("pip install --no-compile --target build/windows-dependencies", source)
                self.assertIn("--culture all", source)

    def test_windows_workflows_install_required_wix_extensions(self) -> None:
        extensions = (
            "WixToolset.UI.wixext/5.0.2",
            "WixToolset.Util.wixext/5.0.2",
        )
        for workflow in (
            ".github/workflows/windows.yml",
            ".github/workflows/release.yml",
        ):
            source = read(workflow)
            for extension in extensions:
                with self.subTest(workflow=workflow, extension=extension):
                    self.assertIn(
                        f"wix extension add -g {extension}",
                        source,
                    )

    def test_windows_workflows_verify_the_current_service_module(self) -> None:
        for workflow in (
            ".github/workflows/windows.yml",
            ".github/workflows/release.yml",
        ):
            source = read(workflow)
            with self.subTest(workflow=workflow):
                self.assertIn(
                    "Get-ChildItem $target -Recurse -File -Filter daemon.py",
                    source,
                )
                self.assertNotIn("-Filter hardware_service.py", source)

    def test_windows_ci_uses_the_packaged_python_runtime(self) -> None:
        workflow = read('.github/workflows/windows.yml')
        self.assertIn("python-version: '3.13'", workflow)
        self.assertNotIn('matrix.python', workflow)
        self.assertNotIn('strategy:', workflow)

    def test_windows_workflow_runs_the_complete_current_windows_suite(self) -> None:
        windows = read(".github/workflows/windows.yml")
        self.assertIn(
            "python -m unittest discover -s tests -p 'test_windows*.py' -v",
            windows,
        )
        self.assertIn(
            "python -m compileall -q src/panasonic_posture/windows",
            windows,
        )
        self.assertIn(
            "python -m unittest tests.test_localization tests.test_i18n_check -v",
            windows,
        )
        for retired in (
            "test_windows_switch.py",
            "test_windows_tablet_mode.py",
            "test_windows_tablet_display.py",
        ):
            with self.subTest(retired=retired):
                self.assertNotIn(retired, windows)

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
        for dependency in ("package-arch", "package-deb", "package-rpm", "package-windows"):
            self.assertIn(f"      - {dependency}", release)
        self.assertIn("contents: write", release)
        self.assertIn("gh release upload", release)
        self.assertIn("gh release create", release)
        self.assertIn("SHA256SUMS", release)

    def test_new_release_uses_reviewed_notes_for_its_tag(self) -> None:
        release = self.workflow.split("  release:", 1)[1].split("  aur:", 1)[0]
        self.assertIn("ref: ${{ env.SOURCE_REF }}", release)
        self.assertIn('notes="docs/releases/$TAG.md"', release)
        self.assertIn('--notes-file "$notes"', release)
        self.assertNotIn("--generate-notes", release)

    def test_aur_push_uses_generated_metadata_and_a_secret(self) -> None:
        aur = self.workflow.split("  aur:", 1)[1]
        self.assertIn("needs: release", aur)
        self.assertIn("name: aur-metadata", aur)
        self.assertIn("secrets.AUR_SSH_PRIVATE_KEY", aur)
        self.assertIn("packaging/linux/scripts/publish-aur.sh aur-metadata", aur)
        self.assertIn("name: aur-bin-metadata", aur)
        self.assertIn("AUR_PACKAGE: panasonic-posture-manager-bin", aur)
        self.assertIn("packaging/linux/scripts/publish-aur.sh aur-bin-metadata", aur)
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", self.workflow)

    def test_arch_job_builds_and_checks_the_aur_bin_package(self) -> None:
        arch = self.workflow.split("  package-arch:", 1)[1].split("  package-deb:", 1)[0]
        self.assertIn("name: aur-bin-metadata", self.workflow)
        self.assertIn("generate-aur-bin.sh", arch)
        self.assertIn("check_aur_bin.py", arch)
        self.assertIn("SRCDEST=/tmp/ppm-arch/packaging/linux/aur-bin/srcdest", arch)
        self.assertIn("panasonic-posture-manager-bin-[0-9]*.pkg.tar.zst", arch)
        self.assertIn("makepkg --printsrcinfo > SRCINFO", arch)
        self.assertLess(
            arch.index("makepkg --printsrcinfo > SRCINFO"),
            arch.index("check_aur_bin.py"),
        )
        self.assertEqual(arch.count("'"), 2)

    def test_aur_host_key_is_pinned(self) -> None:
        known_hosts = read("packaging/linux/ci/aur_known_hosts")
        self.assertEqual(len(known_hosts.strip().splitlines()), 1)
        self.assertRegex(
            known_hosts,
            r"^aur\.archlinux\.org ssh-ed25519 [A-Za-z0-9+/=]+$",
        )


if __name__ == "__main__":
    unittest.main()
