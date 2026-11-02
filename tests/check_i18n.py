#!/usr/bin/env python3
"""Verify that every translatable string is covered by the gettext catalog.

The check is part of ``make check`` and uses only the Python standard library.

What is checked
---------------
* source scan: ``_('...')`` / ``_("...")`` and ``gettext(...)`` literals in the
  Python session package and the GJS UI files (``self._(...)`` included);
* catalog: every referenced msgid has a non-empty, non-fuzzy ``msgstr`` and
  keeps the same printf placeholders; duplicate msgids are reported;
* catalog cleanliness: msgids that are no longer referenced are an error
  (``--allow-stale`` demotes this to a warning);
* parity: the Python error mapping (``session/errors.py``) and the mirrored
  JS mapping (``data/ui/errors.js``) cover exactly the same stable codes with
  exactly the same English msgids, and both cover all ``protocol.ErrorCode``
  values;
* ``po/POTFILES.in`` lists exactly the files that contain translatable
  strings.

Exit code: 0 when everything is fine, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOTS = (Path("src/panasonic_posture/session"), Path("data/ui"))
SOURCE_SUFFIXES = {".py", ".js"}
POTFILES = Path("po/POTFILES.in")
CATALOGS = (Path("po/zh_CN.po"),)
PROTOCOL = Path("src/panasonic_posture/protocol.py")
PY_ERRORS = Path("src/panasonic_posture/session/errors.py")
JS_ERRORS = Path("data/ui/errors.js")

#: Entries that are intentionally kept although currently unreferenced.
STALE_WHITELIST: frozenset[str] = frozenset()

_STRING = r"""(?P<quote>['"])(?P<text>(?:\\.|(?!\1).)*?)(?P=quote)"""
_CALL_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:_|gettext)\(\s*" + _STRING + r"\s*[,)]", re.S
)
_PY_MAP_RE = re.compile(r"ErrorCode\.([A-Z_]+):\s*_\(\s*" + _STRING + r"\s*\)", re.S)
_JS_MAP_RE = re.compile(r"'([a-z][a-z0-9-]+)':\s*_\(\s*" + _STRING + r"\s*\)", re.S)
_PLACEHOLDER_RE = re.compile(r"%[a-zA-Z%]")


# ---------------------------------------------------------------------------
# Source scanning
# ---------------------------------------------------------------------------


def _unescape(quote: str, text: str) -> str:
    try:
        return ast.literal_eval(quote + text + quote)
    except (SyntaxError, ValueError):
        return (
            text.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\'", "'")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


def source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        files.extend(
            path
            for path in sorted(base.rglob("*"))
            if path.is_file() and path.suffix in SOURCE_SUFFIXES
        )
    return files


def extracted_msgids(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [
        _unescape(match.group("quote"), match.group("text"))
        for match in _CALL_RE.finditer(text)
    ]


# ---------------------------------------------------------------------------
# PO parsing
# ---------------------------------------------------------------------------


@dataclass
class CatalogEntry:
    msgid: str
    msgstr: str
    fuzzy: bool
    line: int


def _po_unquote(text: str) -> str:
    return (
        text.replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace('\\"', '"')
        .replace("\\\\", "\\")
    )


def parse_po(path: Path) -> tuple[list[CatalogEntry], list[str]]:
    entries: list[CatalogEntry] = []
    errors: list[str] = []
    current = {"msgid": "", "msgstr": "", "fuzzy": False, "line": 0}
    part = None
    have_entry = False

    def flush() -> None:
        nonlocal current, have_entry
        if not have_entry:
            return
        if current["msgid"]:
            entries.append(
                CatalogEntry(
                    msgid=current["msgid"],
                    msgstr=current["msgstr"],
                    fuzzy=current["fuzzy"],
                    line=current["line"],
                )
            )
        current = {"msgid": "", "msgstr": "", "fuzzy": False, "line": 0}
        have_entry = False

    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            flush()
            part = None
            continue
        if line.startswith("#,") or line.startswith("#"):
            if "fuzzy" in line:
                current["fuzzy"] = True
            continue
        if line.startswith("msgid "):
            if have_entry and current["msgstr"]:
                flush()
            if not have_entry:
                current["line"] = number
                have_entry = True
            part = "msgid"
            value = re.fullmatch(r'msgid "(.*)"', line)
            if value is None:
                errors.append(f"{path}:{number}: malformed msgid line")
                continue
            current["msgid"] += _po_unquote(value.group(1))
        elif line.startswith("msgstr "):
            part = "msgstr"
            value = re.fullmatch(r'msgstr "(.*)"', line)
            if value is None:
                errors.append(f"{path}:{number}: malformed msgstr line")
                continue
            current["msgstr"] += _po_unquote(value.group(1))
        elif line.startswith('"'):
            value = re.fullmatch(r'"(.*)"', line)
            if value is None or part is None:
                errors.append(f"{path}:{number}: malformed continuation line")
                continue
            current[part] += _po_unquote(value.group(1))
        else:
            errors.append(f"{path}:{number}: unexpected line {line[:40]!r}")
    flush()
    return entries, errors


# ---------------------------------------------------------------------------
# Mapping parity
# ---------------------------------------------------------------------------


def _error_code_values() -> dict[str, str]:
    text = (REPO_ROOT / PROTOCOL).read_text(encoding="utf-8")
    block = re.search(r"class ErrorCode:\n(.*?)\n\nALL_ERROR_CODES", text, re.S)
    if block is None:
        return {}
    return dict(re.findall(r'^[ \t]+([A-Z_]+) = "([^"]+)"$', block.group(1), re.M))


def _python_error_mapping() -> dict[str, str]:
    text = (REPO_ROOT / PY_ERRORS).read_text(encoding="utf-8")
    return {
        name: _unescape(match.group("quote"), match.group("text"))
        for match in _PY_MAP_RE.finditer(text)
        for name in [match.group(1)]
    }


def _js_error_mapping() -> dict[str, str]:
    text = (REPO_ROOT / JS_ERRORS).read_text(encoding="utf-8")
    return {
        name: _unescape(match.group("quote"), match.group("text"))
        for match in _JS_MAP_RE.finditer(text)
        for name in [match.group(1)]
    }


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------


def run(allow_stale: bool = False) -> int:
    errors: list[str] = []
    warnings: list[str] = []

    # 1. sources -----------------------------------------------------------
    occurrences: dict[str, set[str]] = {}
    files_with_strings: set[str] = set()
    for path in source_files():
        relative = path.relative_to(REPO_ROOT).as_posix()
        for msgid in extracted_msgids(path):
            occurrences.setdefault(msgid, set()).add(relative)
            files_with_strings.add(relative)
    if not occurrences:
        errors.append("no translatable strings were found; source scan is broken")

    # 2. catalogs ----------------------------------------------------------
    total_translated = 0
    for catalog_path in CATALOGS:
        full_path = REPO_ROOT / catalog_path
        if not full_path.is_file():
            errors.append(f"missing translation catalog {catalog_path}")
            continue
        entries, parse_errors = parse_po(full_path)
        errors.extend(parse_errors)
        seen: Counter[str] = Counter(entry.msgid for entry in entries)
        duplicates = sorted(msgid for msgid, count in seen.items() if count > 1)
        if duplicates:
            errors.append(f"{catalog_path}: duplicate msgids: {duplicates}")
        catalog = {entry.msgid: entry for entry in entries}
        translated = 0
        for msgid in sorted(occurrences):
            entry = catalog.get(msgid)
            if entry is None:
                errors.append(f"{catalog_path}: missing translation for {msgid!r}")
                continue
            if entry.fuzzy:
                errors.append(f"{catalog_path}:{entry.line}: fuzzy entry for {msgid!r}")
            if not entry.msgstr:
                errors.append(f"{catalog_path}:{entry.line}: empty msgstr for {msgid!r}")
                continue
            if Counter(_PLACEHOLDER_RE.findall(msgid)) != Counter(
                _PLACEHOLDER_RE.findall(entry.msgstr)
            ):
                errors.append(
                    f"{catalog_path}:{entry.line}: placeholders differ for {msgid!r}"
                )
            translated += 1
        stale = sorted(set(catalog) - set(occurrences) - STALE_WHITELIST)
        if stale:
            message = f"{catalog_path}: {len(stale)} unreferenced msgid(s): {stale}"
            if allow_stale:
                warnings.append(message)
            else:
                errors.append(message + " (use --allow-stale to permit)")
        total_translated += translated
        print(
            f"i18n: {len(occurrences)} msgids referenced, "
            f"{translated} translated in {catalog_path}, "
            f"{len(stale)} unreferenced"
        )

    # 3. POTFILES ----------------------------------------------------------
    potfiles_path = REPO_ROOT / POTFILES
    if not potfiles_path.is_file():
        errors.append(f"missing {POTFILES}")
    else:
        listed = [
            line.strip()
            for line in potfiles_path.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        for entry in listed:
            if not (REPO_ROOT / entry).is_file():
                errors.append(f"{POTFILES}: listed file does not exist: {entry}")
        missing = sorted(files_with_strings - set(listed))
        if missing:
            errors.append(f"{POTFILES}: missing files with translatable strings: {missing}")
        unused = sorted(set(listed) - files_with_strings)
        if unused:
            errors.append(f"{POTFILES}: listed but no translatable strings: {unused}")

    # 4. error code parity -------------------------------------------------
    codes = _error_code_values()
    python_mapping = _python_error_mapping()
    js_mapping = _js_error_mapping()
    if not codes:
        errors.append(f"{PROTOCOL}: cannot read the ErrorCode constants")
    if set(python_mapping) != set(codes):
        errors.append(
            "session/errors.py mapping does not cover exactly protocol.ErrorCode: "
            f"missing={sorted(set(codes) - set(python_mapping))} "
            f"unknown={sorted(set(python_mapping) - set(codes))}"
        )
    if set(js_mapping) != set(codes.values()):
        errors.append(
            "data/ui/errors.js mapping does not match protocol.ErrorCode values: "
            f"missing={sorted(set(codes.values()) - set(js_mapping))} "
            f"unknown={sorted(set(js_mapping) - set(codes.values()))}"
        )
    python_by_code = {codes[name]: text for name, text in python_mapping.items() if name in codes}
    if python_by_code != js_mapping:
        differing = sorted(
            code
            for code in set(python_by_code) | set(js_mapping)
            if python_by_code.get(code) != js_mapping.get(code)
        )
        errors.append(f"error message text differs between Python and JS: {differing}")

    # 5. report ------------------------------------------------------------
    for warning in warnings:
        print(f"i18n warning: {warning}", file=sys.stderr)
    if errors:
        for error in errors:
            print(f"i18n error: {error}", file=sys.stderr)
        print(f"i18n checks failed ({len(errors)} error(s))", file=sys.stderr)
        return 1
    print(
        f"i18n checks passed: {len(occurrences)} msgids, "
        f"{total_translated} translated, 0 missing, 0 fuzzy"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="only warn about catalog entries that are no longer referenced",
    )
    arguments = parser.parse_args(argv)
    return run(allow_stale=arguments.allow_stale)


if __name__ == "__main__":
    sys.exit(main())
