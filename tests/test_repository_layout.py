"""Platform layout contracts for native and packaging assets."""

import ast
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
    def test_script_source_and_sibling_paths_resolve_without_running_scripts(self):
        # Execute only path expressions: importing these scripts can contact
        # real services, alter modes or require administrator privileges.
        scripts = {
            'integration/linux/check_agent.py': ('REPO_ROOT', 'SRC_DIR', 'DAEMON'),
            'integration/linux/check_dbus.py': ('REPO_ROOT', 'SRC_DIR', 'DAEMON'),
            'integration/linux/session_daemon.py': ('REPO_ROOT', 'SRC_DIR'),
            'package/linux/check_package.py': ('REPO_ROOT',),
            'manual/linux/check_live.py': ('REPO_ROOT', '_SRC_DIR'),
            'manual/linux/check_faults.py': ('REPO_ROOT', '_SRC_DIR'),
            'manual/linux/manual_reset_guard.py': ('REPO_ROOT', '_SRC_DIR'),
            'manual/linux/check_absent_switch.py': ('REPO_ROOT', '_SRC_DIR', 'HELPER'),
            'manual/windows/windows_hardware_smoke.py': (),
            'manual/windows/windows_etw_switch_smoke.py': (),
            'manual/windows/windows_launcher_smoke.py': ('repository', 'package', 'helper'),
        }
        for relative, names in scripts.items():
            path = ROOT / 'tests' / relative
            with self.subTest(script=relative):
                tree = ast.parse(path.read_text(encoding='utf-8'))
                scope = {'Path': Path, '__file__': str(path)}
                for name in names:
                    node = next(node for node in ast.walk(tree)
                                if isinstance(node, ast.Assign)
                                and any(isinstance(target, ast.Name) and target.id == name
                                        for target in node.targets))
                    scope[name] = eval(compile(ast.Expression(node.value), str(path), 'eval'), scope)
                    if name in ('REPO_ROOT', 'repository'):
                        self.assertEqual(scope[name], ROOT)
                    else:
                        self.assertTrue(scope[name].exists(), scope[name])
                if not names:
                    insertion = next(node for node in ast.walk(tree)
                                     if isinstance(node, ast.Call)
                                     and isinstance(node.func, ast.Attribute)
                                     and node.func.attr == 'insert')
                    source = Path(eval(compile(ast.Expression(insertion.args[1]), str(path), 'eval'), scope))
                    self.assertEqual(source, ROOT / 'src')

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
        self.assertEqual(payload_directories(assets), {"common", "windows"})
        self.assertTrue((assets / "common" / "postures" / "laptop.svg").is_file())
        self.assertTrue(
            (assets / "windows" / "icons" / "work.mmoe.panasonic.PostureManager.ico").is_file()
        )

    def test_shared_artwork_has_one_authoritative_copy(self):
        assets = ROOT / 'data/assets'
        common = assets / 'common'
        self.assertEqual(payload_directories(common), {'branding', 'icons', 'postures'})
        shared = {path.relative_to(common).as_posix(): path.read_bytes()
                  for path in common.rglob('*') if path.is_file()}
        self.assertEqual(len(shared), 7)
        for path in (assets / 'windows').rglob('*'):
            if path.is_file():
                with self.subTest(path=path):
                    self.assertNotIn(path.relative_to(assets / 'windows').as_posix(), shared)
                    self.assertNotIn(path.read_bytes(), shared.values())

    def test_linux_validation_scripts_have_platform_homes(self):
        for kind, names in {
            'integration': ('check_agent.py', 'check_dbus.py', 'session_daemon.py'),
            'package': ('check_package.py', 'check_aur_bin.py'),
        }.items():
            with self.subTest(kind=kind):
                base = ROOT / 'tests' / kind
                self.assertEqual(payload_directories(base), {'linux'})
                self.assertFalse(list(base.glob('*.py')))
                for name in names:
                    self.assertTrue((base / 'linux' / name).is_file())

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
        self.assertIn("tests/integration/linux/check_dbus.py", makefile)
        self.assertIn("tests/integration/linux/check_agent.py", makefile)
        self.assertIn("tests/package/linux/check_package.py", makefile)

        workflow = (
            ROOT / ".github" / "workflows" / "release.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("packaging/linux/aur/PKGBUILD", workflow)
        self.assertIn("packaging/common/release_version.py", workflow)
        self.assertIn("packaging/windows/build_msi.py", workflow)
        self.assertIn("tests/package/linux/check_aur_bin.py", workflow)
        self.assertIn("assets/postures/laptop.svg", workflow)
        windows = (
            ROOT / ".github" / "workflows" / "windows.yml"
        ).read_text(encoding="utf-8")
        self.assertNotIn("panasonic_posture/fluent", windows)
        self.assertIn("tests.test_repository_layout tests.test_platform_imports", windows)
        self.assertTrue(
            (ROOT / ".github" / "workflows" / "linux.yml").is_file()
        )


if __name__ == "__main__":
    unittest.main()
