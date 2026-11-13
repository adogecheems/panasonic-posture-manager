"""Static import boundaries for the shared, Linux and Windows code paths."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "src"
PACKAGE_ROOT = SOURCE / "panasonic_posture"
PACKAGE = "panasonic_posture"

WINDOWS_PREFIXES = (
    "panasonic_posture.windows",
    "panasonic_posture.fluent",
)
LINUX_MODULES = frozenset(
    {
        "panasonic_posture.cli",
        "panasonic_posture.protocol",
        "panasonic_posture.linux_contract",
        "panasonic_posture.state_codec",
        "panasonic_posture.hardware.iio",
        "panasonic_posture.hardware.probe",
        "panasonic_posture.hardware.sysfs",
        "panasonic_posture.service.access",
        "panasonic_posture.service.actuator",
        "panasonic_posture.service.application",
        "panasonic_posture.service.callbacks",
        "panasonic_posture.service.daemon",
        "panasonic_posture.service.sensor",
        "panasonic_posture.service.sensor_sources",
        "panasonic_posture.service.settings",
        "panasonic_posture.session.agent",
        "panasonic_posture.session.client",
        "panasonic_posture.session.dbus_errors",
        "panasonic_posture.session.desktop",
        "panasonic_posture.session.display",
        "panasonic_posture.session.errors",
        "panasonic_posture.session.journal",
        "panasonic_posture.session.lockstate",
        "panasonic_posture.session.notify",
        "panasonic_posture.session.plasma",
        "panasonic_posture.session.screen_context",
    }
)
LINUX_PREFIXES = ("panasonic_posture.ui",)


def _module_parts(path: Path) -> tuple[list[str], bool]:
    parts = list(path.relative_to(SOURCE).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return parts, is_package


def _resolve_from(
    parts: list[str], is_package: bool, node: ast.ImportFrom
) -> str:
    package = parts if is_package else parts[:-1]
    if node.level:
        package = package[: len(package) - (node.level - 1)]
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
                imported.update(f"{base}.{alias.name}" for alias in node.names)
    return imported


def _matches(module: str, exact: frozenset[str], prefixes: tuple[str, ...]) -> bool:
    return module in exact or any(
        module == prefix or module.startswith(prefix + ".")
        for prefix in prefixes
    )


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


class PlatformImportTests(unittest.TestCase):
    def test_windows_code_does_not_import_linux_adapters(self):
        violations: list[str] = []
        roots = (PACKAGE_ROOT / "windows", PACKAGE_ROOT / "fluent")
        for root in roots:
            for path in _python_files(root):
                for module in _imported_modules(path):
                    if module == "gi" or module.startswith("gi."):
                        violations.append(f"{path.relative_to(SOURCE)} imports {module}")
                    elif _matches(module, LINUX_MODULES, LINUX_PREFIXES):
                        violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_linux_adapters_do_not_import_windows_code(self):
        linux_files = {
            PACKAGE_ROOT / "cli.py",
            PACKAGE_ROOT / "protocol.py",
            PACKAGE_ROOT / "linux_contract.py",
            PACKAGE_ROOT / "state_codec.py",
            *(
                PACKAGE_ROOT / "hardware" / name
                for name in ("iio.py", "probe.py", "sysfs.py")
            ),
            *(
                PACKAGE_ROOT / "service" / name
                for name in (
                    "access.py",
                    "actuator.py",
                    "application.py",
                    "callbacks.py",
                    "daemon.py",
                    "sensor.py",
                    "sensor_sources.py",
                    "settings.py",
                )
            ),
            *(
                PACKAGE_ROOT / "session" / name
                for name in (
                    "agent.py",
                    "client.py",
                    "dbus_errors.py",
                    "desktop.py",
                    "display.py",
                    "errors.py",
                    "journal.py",
                    "lockstate.py",
                    "notify.py",
                    "plasma.py",
                    "screen_context.py",
                )
            ),
            *_python_files(PACKAGE_ROOT / "ui"),
        }
        violations: list[str] = []
        for path in sorted(linux_files):
            for module in _imported_modules(path):
                if _matches(module, frozenset(), WINDOWS_PREFIXES):
                    violations.append(f"{path.relative_to(SOURCE)} imports {module}")
        self.assertEqual(violations, [])

    def test_windows_and_linux_path_sets_are_explicit(self):
        windows = set(_python_files(PACKAGE_ROOT / "windows"))
        fluent = set(_python_files(PACKAGE_ROOT / "fluent"))
        self.assertTrue(windows)
        self.assertTrue(fluent)
        self.assertFalse(windows & fluent)
        self.assertTrue((PACKAGE_ROOT / "windows" / "hardware_runtime.py").is_file())
        self.assertTrue((PACKAGE_ROOT / "ui" / "app.py").is_file())


if __name__ == "__main__":
    unittest.main()
