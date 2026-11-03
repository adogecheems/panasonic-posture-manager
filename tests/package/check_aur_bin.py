#!/usr/bin/env python3
"""Validate a built AUR -bin package against its source Arch package."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

SOURCE_PACKAGE = "panasonic-posture-manager"
BIN_PACKAGE = "panasonic-posture-manager-bin"
META_FILES = {".PKGINFO", ".BUILDINFO", ".MTREE", ".INSTALL"}


class CheckError(RuntimeError):
    """Raised when the prebuilt package does not match its source package."""


def run(command: Sequence[str], *, text: bool = True) -> str | bytes:
    result = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=text,
    )
    if result.returncode != 0:
        output = result.stdout.strip()
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        raise CheckError(f"command failed ({' '.join(command)}): {output}")
    return result.stdout


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckError(message)


def parse_key_values(text: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#") or " = " not in line:
            continue
        key, value = line.split(" = ", 1)
        values.setdefault(key, []).append(value)
    return values


def parse_srcinfo(path: Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    in_base = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("pkgbase = "):
            in_base = True
            values.setdefault("pkgbase", []).append(line.split(" = ", 1)[1])
            continue
        if line.startswith("pkgname = "):
            break
        if not in_base or not line.startswith("\t") or " = " not in line:
            continue
        key, value = line.strip().split(" = ", 1)
        values.setdefault(key, []).append(value)
    return values


def archive_text(archive: Path, name: str) -> str:
    return run(["bsdtar", "-xOf", str(archive), name], text=True).rstrip("\n")


def archive_entries(archive: Path) -> list[str]:
    return [line for line in run(["bsdtar", "-tf", str(archive)]).splitlines() if line]


def extract_usr(archive: Path, destination: Path) -> Path:
    destination.mkdir()
    run(["bsdtar", "-xf", str(archive), "-C", str(destination), "usr"])
    return destination / "usr"


def inventory(root: Path) -> dict[str, tuple[object, ...]]:
    entries: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            entries[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            entries[relative] = ("directory", mode)
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries[relative] = ("file", mode, digest)
        else:
            entries[relative] = ("other", mode)
    return entries


def check_source_metadata(metadata: dict[str, list[str]], source: Path) -> str:
    require(metadata.get("pkgbase") == [BIN_PACKAGE], "SRCINFO pkgbase is incorrect")
    require(metadata.get("pkgver"), "SRCINFO has no pkgver")
    require(metadata.get("pkgrel"), "SRCINFO has no pkgrel")
    version = f"{metadata['pkgver'][0]}-{metadata['pkgrel'][0]}"
    source_names = metadata.get("source", [])
    require(
        any(name.split("::", 1)[-1].endswith(source.name) for name in source_names),
        "SRCINFO source does not reference the release archive",
    )
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    require(checksum in metadata.get("sha256sums", []), "SRCINFO checksum is incorrect")
    return version


def check_binary(
    source: Path,
    binary: Path,
    metadata_dir: Path,
) -> None:
    require(source.is_file(), f"source package not found: {source}")
    require(binary.is_file(), f"prebuilt package not found: {binary}")

    source_info = parse_key_values(archive_text(source, ".PKGINFO"))
    binary_info = parse_key_values(archive_text(binary, ".PKGINFO"))
    require(binary_info.get("pkgname") == [BIN_PACKAGE], "unexpected prebuilt package name")
    require(binary_info.get("pkgver") == source_info.get("pkgver"), "package versions differ")
    require(binary_info.get("arch") == ["x86_64"], "prebuilt package is not x86_64")
    require(
        "pkgtype=debug" not in " ".join(binary_info.get("xdata", [])),
        "a -debug package was supplied",
    )
    for field in ("depend", "optdepend"):
        require(
            binary_info.get(field, []) == source_info.get(field, []),
            f"prebuilt package {field}s differ from the source package",
        )
    require(
        binary_info.get("provides") == [SOURCE_PACKAGE],
        "prebuilt package does not provide the source package",
    )
    require(
        binary_info.get("conflict") == [SOURCE_PACKAGE],
        "prebuilt package does not conflict with the source package",
    )

    srcinfo = metadata_dir / "SRCINFO"
    if not srcinfo.is_file():
        srcinfo = metadata_dir / ".SRCINFO"
    require(srcinfo.is_file(), f"SRCINFO not found in {metadata_dir}")
    metadata = parse_srcinfo(srcinfo)
    expected_version = check_source_metadata(metadata, source)
    require(binary_info.get("pkgver") == [expected_version], "SRCINFO version is incorrect")

    install_name = metadata.get("install", [])
    require(len(install_name) == 1, "SRCINFO must name exactly one install file")
    install_file = metadata_dir / install_name[0]
    require(install_file.is_file(), f"install script not found: {install_file}")
    require(
        archive_text(binary, ".INSTALL") == install_file.read_text(encoding="utf-8").rstrip("\n"),
        "packaged .INSTALL differs from the generated install script",
    )

    entries = archive_entries(binary)
    require(not (META_FILES - set(entries)), "prebuilt package is missing metadata files")
    for entry in entries:
        normalized = entry.rstrip("/")
        require(
            normalized in META_FILES or normalized == "usr" or normalized.startswith("usr/"),
            f"unexpected archive path: {entry}",
        )

    with tempfile.TemporaryDirectory(prefix="ppm-aur-bin-check.") as temporary:
        root = Path(temporary)
        source_usr = extract_usr(source, root / "source")
        binary_usr = extract_usr(binary, root / "binary")
        require(
            inventory(source_usr) == inventory(binary_usr),
            "prebuilt package contents differ from the source package",
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    try:
        check_binary(arguments.source, arguments.binary, arguments.metadata)
    except (CheckError, OSError) as error:
        print(f"AUR -bin package check failed: {error}", file=sys.stderr)
        return 1
    print("AUR -bin package checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
