"""Static import boundaries for the shared, Linux and Windows code paths."""

from __future__ import annotations

import ast
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SOURCE / "panasonic_posture"
PACKAGE = "panasonic_posture"

WINDOWS_PREFIXES = (
    "panasonic_posture.windows",
)
LINUX_PREFIXES = ("panasonic_posture.linux",)
LINUX_DOMAIN_DEPENDENCIES = {
    "app": frozenset({"app", "ui"}),
    "contracts": frozenset({"contracts"}),
    "hardware": frozenset({"hardware", "contracts"}),
    "service": frozenset({"service", "contracts", "hardware"}),
    "session": frozenset({"session", "contracts"}),
    "ui": frozenset({"ui", "contracts", "session"}),
}
WINDOWS_DOMAIN_DEPENDENCIES = {
    "app": frozenset({"app", "common"}),
    "common": frozenset({"common"}),
    "hardware": frozenset({"hardware", "common", "ipc"}),
    "ipc": frozenset({"ipc", "common"}),
    "service": frozenset({"service", "common", "hardware", "ipc"}),
    "session": frozenset({"session", "common", "hardware", "ipc"}),
    "ui": frozenset({"ui", "app", "common", "ipc"}),
}


def _module_parts(path: Path) -> tuple[list[str], bool]:
    parts = list(path.relative_to(SOURCE).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return parts, is_package


def _resolve_from(
    parts: list[str], is_package: bool, node: ast.ImportFrom
) -> str:
    if node.level:
        package = parts if is_package else parts[:-1]
        package = package[: len(package) - (node.level - 1)]
    else:
        package = []
    if node.module:
        package.extend(node.module.split("."))
    return ".".join(package)


def _imported_modules(path: Path) -> set[str]:
    parts, is_package = _module_parts(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from(parts, is_package, node)
            if not base:
                continue
            imported.add(base)
            imported.update(
                candidate
                for alias in node.names
                if _package_module_exists(candidate := f"{base}.{alias.name}")
            )
    return imported


def _matches(module: str, exact: frozenset[str], prefixes: tuple[str, ...]) -> bool:
    return module in exact or any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in prefixes
    )


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _package_module_exists(module: str) -> bool:
    if module == PACKAGE:
        return (PACKAGE_ROOT / "__init__.py").is_file()
    if not module.startswith(PACKAGE + "."):
        return False
    relative = module.removeprefix(PACKAGE + ".").split(".")
    base = PACKAGE_ROOT.joinpath(*relative)
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


class ImportScannerTests(unittest.TestCase):
    def test_from_imports_include_existing_child_modules(self):
        for source in (
            "from panasonic_posture.windows import service as backend",
            "from .. import service as backend",
        ):
            with self.subTest(source=source), patch.object(Path, "read_text", return_value=source):
                modules = _imported_modules(PACKAGE_ROOT / "windows" / "ui" / "probe.py")
                self.assertEqual(modules, {
                    "panasonic_posture.windows", "panasonic_posture.windows.service",
                })

    def test_package_relative_imports_use_the_package_directory(self):
        with patch.object(Path, "read_text", return_value="from . import ui"):
            self.assertEqual(
                _imported_modules(PACKAGE_ROOT / "windows" / "__init__.py"),
                {"panasonic_posture.windows", "panasonic_posture.windows.ui"},
            )

    def test_multiple_relative_imports_keep_the_original_package(self):
        with patch.object(Path, "read_text", return_value=(
            "from .ui import app\nfrom . import hardware\n"
        )):
            self.assertEqual(
                _imported_modules(PACKAGE_ROOT / "windows" / "__init__.py"),
                {"panasonic_posture.windows.ui", "panasonic_posture.windows.ui.app",
                 "panasonic_posture.windows", "panasonic_posture.windows.hardware"},
            )

    def test_attributes_and_star_imports_are_not_invented_as_modules(self):
        with patch.object(Path, "read_text", return_value=(
            "from panasonic_posture.model import Target as Goal\n"
            "from panasonic_posture.model import *\n"
            "import panasonic_posture.windows.service as backend\n"
        )):
            self.assertEqual(
                _imported_modules(PACKAGE_ROOT / "probe.py"),
                {"panasonic_posture.model", "panasonic_posture.windows.service"},
            )

    def test_invalid_source_fails_the_scan(self):
        with patch.object(Path, "read_text", return_value="from . import"), self.assertRaises(SyntaxError):
            _imported_modules(PACKAGE_ROOT / "probe.py")


class PlatformImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Share a fresh scan within this suite, without retaining stale results
        # when the suite is run again in the same interpreter.
        cls.imports = {
            path: _imported_modules(path) for path in _python_files(PACKAGE_ROOT)
        }

    def test_windows_code_does_not_import_linux_adapters(self):
        violations: list[str] = []
        for path in _python_files(PACKAGE_ROOT / "windows"):
            for module in self.imports[path]:
                if module == "gi" or module.startswith("gi."):
                    violations.append(f"{path.relative_to(SOURCE)} imports {module}")
                elif _matches(module, frozenset(), LINUX_PREFIXES):
                    violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_linux_adapters_do_not_import_windows_code(self):
        violations: list[str] = []
        for path in _python_files(PACKAGE_ROOT / "linux"):
            for module in self.imports[path]:
                if _matches(module, frozenset(), WINDOWS_PREFIXES):
                    violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_shared_code_does_not_import_platform_adapters(self):
        violations: list[str] = []
        for path in _python_files(PACKAGE_ROOT):
            parts = path.relative_to(PACKAGE_ROOT).parts
            if parts[0] in {"linux", "windows"}:
                continue
            for module in self.imports[path]:
                if _matches(module, frozenset(), WINDOWS_PREFIXES + LINUX_PREFIXES):
                    violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_windows_domains_follow_the_dependency_direction(self):
        violations: list[str] = []
        windows = PACKAGE_ROOT / "windows"
        for path in _python_files(windows):
            parts = path.relative_to(windows).parts
            if len(parts) == 1:
                continue
            source = parts[0]
            allowed = WINDOWS_DOMAIN_DEPENDENCIES[source]
            for module in self.imports[path]:
                if not module.startswith("panasonic_posture.windows."):
                    continue
                target = module.split(".")[2]
                if target not in allowed:
                    violations.append(
                        f"{path.relative_to(SOURCE)} imports {module}"
                    )
        self.assertEqual(violations, [])

    def test_linux_domains_follow_the_dependency_direction(self):
        violations: list[str] = []
        linux = PACKAGE_ROOT / "linux"
        for path in _python_files(linux):
            parts = path.relative_to(linux).parts
            if len(parts) == 1:
                continue
            source = parts[0]
            allowed = LINUX_DOMAIN_DEPENDENCIES[source]
            for module in self.imports[path]:
                if not module.startswith("panasonic_posture.linux."):
                    continue
                target = module.split(".")[2]
                if target not in allowed:
                    violations.append(
                        f"{path.relative_to(SOURCE)} imports {module}"
                    )
        self.assertEqual(violations, [])

    def test_all_package_imports_resolve_to_source_modules(self):
        violations: list[str] = []
        for path in _python_files(PACKAGE_ROOT):
            for module in self.imports[path]:
                if module.startswith(PACKAGE) and not _package_module_exists(module):
                    violations.append(f"{path.relative_to(SOURCE)} imports missing {module}")
        self.assertEqual(violations, [])

    def test_windows_and_linux_path_sets_are_explicit(self):
        windows = set(_python_files(PACKAGE_ROOT / "windows"))
        linux = set(_python_files(PACKAGE_ROOT / "linux"))
        self.assertTrue(windows)
        self.assertTrue(linux)
        self.assertFalse(windows & linux)
        self.assertTrue(
            (PACKAGE_ROOT / "windows" / "service" / "runtime.py").is_file()
        )
        for domain in (
            "app",
            "common",
            "hardware",
            "ipc",
            "service",
            "session",
            "ui",
        ):
            with self.subTest(domain=domain):
                self.assertTrue((PACKAGE_ROOT / "windows" / domain).is_dir())
        for domain in (
            "app",
            "contracts",
            "hardware",
            "service",
            "session",
            "ui",
        ):
            with self.subTest(linux_domain=domain):
                self.assertTrue((PACKAGE_ROOT / "linux" / domain).is_dir())
        self.assertTrue((PACKAGE_ROOT / "windows" / "ui" / "app.py").is_file())
        self.assertTrue((PACKAGE_ROOT / "linux" / "ui" / "app.py").is_file())
        self.assertEqual(
            {path.name for path in (PACKAGE_ROOT / "windows").glob("*.py")},
            {"__init__.py"},
        )
        self.assertEqual(
            {path.name for path in (PACKAGE_ROOT / "linux").glob("*.py")},
            {"__init__.py"},
        )
        self.assertFalse(list((PACKAGE_ROOT / "ui").rglob("*.py")))
        self.assertFalse(list((PACKAGE_ROOT / "fluent").rglob("*.py")))
        self.assertFalse(list((PACKAGE_ROOT / "hardware").rglob("*.py")))

    def test_packaging_uses_the_linux_package_paths(self):
        project = tomllib.loads(
            (SOURCE.parent / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["scripts"],
            {
                "panasonic-posture-manager": "panasonic_posture.linux.app.cli:launch_ui",
                "panasonic-posture-manager-probe": (
                    "panasonic_posture.linux.hardware.probe:main"
                ),
            },
        )
        self.assertEqual(
            project["tool"]["setuptools"]["package-data"],
            {"panasonic_posture.linux.contracts.resources": ["*.xml"]},
        )
        self.assertTrue(
            (
                PACKAGE_ROOT
                / "linux"
                / "contracts"
                / "resources"
                / "dbus.xml"
            ).is_file()
        )


if __name__ == "__main__":
    unittest.main()
