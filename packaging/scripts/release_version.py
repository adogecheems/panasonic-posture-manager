#!/usr/bin/env python3
"""Read and validate the single release version used by all packages.

The repository intentionally keeps the version in several user-facing and
packaging files.  Build scripts call this helper before producing artifacts so
a tag cannot publish packages whose metadata disagree.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class VersionError(RuntimeError):
    """Raised when release metadata is inconsistent."""


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def match(pattern: str, text: str, label: str) -> str:
    found = re.search(pattern, text, re.MULTILINE)
    if found is None:
        raise VersionError(f"cannot find {label}")
    return found.group(1)


def release_version() -> str:
    project = tomllib.loads(read("pyproject.toml"))
    version = str(project.get("project", {}).get("version", ""))
    if not version:
        raise VersionError("pyproject.toml has no project.version")

    observed = {
        "src/panasonic_posture/__init__.py": match(
            r'^__version__ = "([^"]+)"$',
            read("src/panasonic_posture/__init__.py"),
            "runtime __version__",
        ),
        "data/ui/settingsPage.js": match(
            r"^export const APP_VERSION = '([^']+)';$",
            read("data/ui/settingsPage.js"),
            "UI APP_VERSION",
        ),
        "packaging/aur/PKGBUILD": match(
            r"^pkgver=(\S+)$",
            read("packaging/aur/PKGBUILD"),
            "PKGBUILD pkgver",
        ),
        "packaging/aur/.SRCINFO": match(
            r"^\s+pkgver = (\S+)$",
            read("packaging/aur/.SRCINFO"),
            ".SRCINFO pkgver",
        ),
    }
    mismatched = {
        "pyproject.toml": version,
        **{path: found for path, found in observed.items() if found != version},
    }
    if len(mismatched) > 1:
        details = ", ".join(f"{path}={found}" for path, found in mismatched.items())
        raise VersionError(f"release versions differ: {details}")

    if f"当前版本：{version}" not in read("README.md"):
        raise VersionError("README.md does not announce the current version")
    if f"## [{version}] - " not in read("CHANGELOG.md"):
        raise VersionError("CHANGELOG.md has no entry for the current version")
    return version


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        help="require an exact v<version> release tag (for example v2.1.0)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        version = release_version()
        if arguments.tag is not None and arguments.tag != f"v{version}":
            raise VersionError(
                f"tag {arguments.tag!r} does not match release v{version}"
            )
    except (OSError, tomllib.TOMLDecodeError, VersionError) as error:
        print(f"release metadata error: {error}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
