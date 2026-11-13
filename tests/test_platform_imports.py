"""Static import boundaries for the shared, Linux and Windows code paths."""

from __future__ import annotations

import ast
from pathlib import Path
import tomllib
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SOURCE / "panasonic_posture"
PACKAGE = "panasonic_posture"

WINDOWS_PREFIXES = (
    "panasonic_posture.windows",
    "panasonic_posture.fluent",
)
LINUX_PREFIXES = ("panasonic_posture.linux",)


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
            if node.module is None:
                imported.update(
                    f"{base}.{alias.name}"
                    for alias in node.names
                    if not (base == PACKAGE and alias.name == "__version__")
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


class PlatformImportTests(unittest.TestCase):
    def test_windows_code_does_not_import_linux_adapters(self):
        violations: list[str] = []
        roots = (PACKAGE_ROOT / "windows", PACKAGE_ROOT / "fluent")
        for root in roots:
            for path in _python_files(root):
                for module in _imported_modules(path):
                    if module == "gi" or module.startswith("gi."):
                        violations.append(f"{path.relative_to(SOURCE)} imports {module}")
                    elif _matches(module, frozenset(), LINUX_PREFIXES):
                        violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_linux_adapters_do_not_import_windows_code(self):
        violations: list[str] = []
        for path in _python_files(PACKAGE_ROOT / "linux"):
            for module in _imported_modules(path):
                if _matches(module, frozenset(), WINDOWS_PREFIXES):
                    violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_shared_code_does_not_import_platform_adapters(self):
        violations: list[str] = []
        for path in _python_files(PACKAGE_ROOT):
            parts = path.relative_to(PACKAGE_ROOT).parts
            if parts[0] in {"linux", "windows", "fluent"}:
                continue
            for module in _imported_modules(path):
                if _matches(module, frozenset(), WINDOWS_PREFIXES + LINUX_PREFIXES):
                    violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_all_package_imports_resolve_to_source_modules(self):
        violations: list[str] = []
        for path in _python_files(PACKAGE_ROOT):
            for module in _imported_modules(path):
                if module.startswith(PACKAGE) and not _package_module_exists(module):
                    violations.append(f"{path.relative_to(SOURCE)} imports missing {module}")
        self.assertEqual(violations, [])

    def test_windows_and_linux_path_sets_are_explicit(self):
        windows = set(_python_files(PACKAGE_ROOT / "windows"))
        fluent = set(_python_files(PACKAGE_ROOT / "fluent"))
        linux = set(_python_files(PACKAGE_ROOT / "linux"))
        self.assertTrue(windows)
        self.assertTrue(fluent)
        self.assertTrue(linux)
        self.assertFalse(windows & fluent)
        self.assertFalse(windows & linux)
        self.assertFalse(fluent & linux)
        self.assertTrue((PACKAGE_ROOT / "windows" / "hardware_runtime.py").is_file())
        self.assertTrue((PACKAGE_ROOT / "linux" / "ui" / "app.py").is_file())
        self.assertFalse(list((PACKAGE_ROOT / "ui").rglob("*.py")))
        self.assertFalse(list((PACKAGE_ROOT / "hardware").rglob("*.py")))

    def test_packaging_uses_the_linux_package_paths(self):
        project = tomllib.loads(
            (SOURCE.parent / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            project["project"]["scripts"],
            {
                "panasonic-posture-manager": "panasonic_posture.linux.cli:launch_ui",
                "panasonic-posture-manager-probe": (
                    "panasonic_posture.linux.hardware.probe:main"
                ),
            },
        )
        self.assertEqual(
            project["tool"]["setuptools"]["package-data"],
            {"panasonic_posture.linux.resources": ["*.xml"]},
        )
        self.assertTrue(
            (PACKAGE_ROOT / "linux" / "resources" / "dbus.xml").is_file()
        )


if __name__ == "__main__":
    unittest.main()
