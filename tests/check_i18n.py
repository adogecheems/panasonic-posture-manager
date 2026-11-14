#!/usr/bin/env python3
"""Verify that every translatable string is covered by the gettext catalog.

The check is part of ``make check`` and uses only the Python standard library.

What is checked
---------------
* source scan: ``_('...')`` / ``_("...")`` and ``gettext(...)`` literals across
  the application package (``self._(...)`` included);
* catalog: every referenced msgid has a non-empty, non-fuzzy ``msgstr`` and
  keeps the same printf and brace-format placeholders; duplicate msgids are
  reported;
* catalog cleanliness: msgids that are no longer referenced are an error
  (``--allow-stale`` demotes this to a warning);
* parity: the Linux session and Windows UI error mappings cover the same protocol error
  codes and source msgids;
* desktop, WiX and polkit metadata have an English source and a non-empty
  ``zh_CN`` value where the format supports localized labels;
* ``po/POTFILES.in`` lists exactly the files that contain translatable
  strings.

Exit code: 0 when everything is fine, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from string import Formatter

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = Path("src/panasonic_posture")
SOURCE_SUFFIXES = {".py"}
POTFILES = Path("po/POTFILES.in")
CATALOGS = (Path("po/zh_CN.po"),)
PROTOCOL = Path("src/panasonic_posture/contracts.py")
PY_ERRORS = Path("src/panasonic_posture/linux/session/errors.py")
WINDOWS_UI_ERRORS = Path("src/panasonic_posture/windows/ui/pages.py")
DESKTOP_ENTRY = Path(
    "packaging/linux/desktop/work.mmoe.panasonic.PostureManager.desktop"
)
MSI_LOCALIZATION_DIR = Path("packaging/windows/msi")
POLKIT_POLICY = Path("packaging/linux/polkit/work.mmoe.panasonic.Posture.policy")
REQUIRED_INSTALLER_STRINGS = frozenset({
    "FeatureTitle",
    "ProductName",
    "ServiceDescription",
    "ServiceDisplayName",
    "ShortcutDescription",
})
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_CJK_RE = re.compile(r"[\u3400-\u9fff]")

#: Entries that are intentionally kept although currently unreferenced.
STALE_WHITELIST: frozenset[str] = frozenset()

_STRING = r"""(?P<quote>['"])(?P<text>(?:\\.|(?!\1).)*?)(?P=quote)"""
_PY_MAP_RE = re.compile(r"ErrorCode\.([A-Z_]+):\s*_\(\s*" + _STRING + r"\s*\)", re.S)
_PRINTF_PLACEHOLDER_RE = re.compile(r"%(?:\([^)]+\))?[a-zA-Z%]")


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
    base = REPO_ROOT / SOURCE_ROOT
    if not base.is_dir():
        return []
    return [
        path
        for path in sorted(base.rglob("*"))
        if path.is_file() and path.suffix in SOURCE_SUFFIXES
    ]


def extracted_msgids(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    msgids: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        name = (
            function.id if isinstance(function, ast.Name)
            else function.attr if isinstance(function, ast.Attribute)
            else None
        )
        if name not in {"_", "gettext"}:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            msgids.append(argument.value)
    return msgids


def placeholders(text: str) -> tuple[Counter[str], Counter[str]]:
    """Return printf and brace-format placeholders used by *text*."""

    printf = Counter(_PRINTF_PLACEHOLDER_RE.findall(text))
    braces = Counter(
        field_name
        for _, field_name, _, _ in Formatter().parse(text)
        if field_name is not None
    )
    return printf, braces


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


def _error_mapping(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    return {
        name: _unescape(match.group("quote"), match.group("text"))
        for match in _PY_MAP_RE.finditer(text)
        for name in [match.group(1)]
    }


def _check_desktop_metadata(errors: list[str]) -> None:
    path = REPO_ROOT / DESKTOP_ENTRY
    if not path.is_file():
        errors.append(f"missing desktop entry {DESKTOP_ENTRY}")
        return
    parser = configparser.RawConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        with path.open(encoding="utf-8") as stream:
            parser.read_file(stream)
    except (configparser.Error, OSError) as error:
        errors.append(f"{DESKTOP_ENTRY}: cannot parse desktop entry: {error}")
        return
    section = "Desktop Entry"
    if not parser.has_section(section):
        errors.append(f"{DESKTOP_ENTRY}: missing [{section}]")
        return
    for key in ("Name", "Name[zh_CN]", "Comment", "Comment[zh_CN]",
                "Keywords", "Keywords[zh_CN]"):
        value = parser.get(section, key, fallback="").strip()
        if not value:
            errors.append(f"{DESKTOP_ENTRY}: missing {key}")
        elif key.endswith("[zh_CN]") and not _CJK_RE.search(value):
            errors.append(f"{DESKTOP_ENTRY}: {key} is not Chinese copy")


def _check_installer_metadata(errors: list[str]) -> None:
    directory = REPO_ROOT / MSI_LOCALIZATION_DIR
    files = sorted(directory.glob("Setup.*.wxl"))
    if not files:
        errors.append(f"missing MSI localization files in {MSI_LOCALIZATION_DIR}")
        return
    cultures: dict[str, dict[str, str]] = {}
    for path in files:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError) as error:
            errors.append(f"{path.relative_to(REPO_ROOT)}: cannot parse: {error}")
            continue
        culture = root.get("Culture", "")
        strings = {
            element.get("Id", ""): element.get("Value", "")
            for element in root
            if element.tag.endswith("String")
        }
        if culture in cultures:
            errors.append(f"{path.relative_to(REPO_ROOT)}: duplicate culture {culture}")
            continue
        cultures[culture] = strings

    required_cultures = {"en-US", "zh-CN"}
    missing_cultures = sorted(required_cultures - set(cultures))
    if missing_cultures:
        errors.append(f"MSI localizations missing cultures: {missing_cultures}")
    if not cultures:
        return
    expected_ids = set(cultures.get("en-US", next(iter(cultures.values()))))
    for culture, strings in cultures.items():
        ids = set(strings)
        if ids != expected_ids:
            errors.append(
                f"MSI localization {culture} has mismatched string IDs: "
                f"missing={sorted(expected_ids - ids)} "
                f"extra={sorted(ids - expected_ids)}"
            )
        missing = sorted(REQUIRED_INSTALLER_STRINGS - ids)
        if missing:
            errors.append(f"MSI localization {culture} misses strings: {missing}")
        for key, value in strings.items():
            if not value.strip():
                errors.append(f"MSI localization {culture} has empty {key}")
        if culture == "zh-CN":
            untranslated = sorted(
                key for key, value in strings.items()
                if value and not _CJK_RE.search(value)
            )
            if untranslated:
                errors.append(
                    f"MSI localization zh-CN has no Chinese copy for: {untranslated}"
                )


def _check_polkit_metadata(errors: list[str]) -> None:
    path = REPO_ROOT / POLKIT_POLICY
    if not path.is_file():
        errors.append(f"missing polkit policy {POLKIT_POLICY}")
        return
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as error:
        errors.append(f"{POLKIT_POLICY}: cannot parse: {error}")
        return
    for name in ("description", "message"):
        elements = list(root.iter(name))
        source = next(
            (element for element in elements if _XML_LANG not in element.attrib),
            None,
        )
        chinese = next(
            (element for element in elements
             if element.get(_XML_LANG) == "zh_CN"),
            None,
        )
        if source is None or not "".join(source.itertext()).strip():
            errors.append(f"{POLKIT_POLICY}: missing English {name}")
        if chinese is None or not _CJK_RE.search("".join(chinese.itertext())):
            errors.append(f"{POLKIT_POLICY}: missing Chinese {name}")


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
        try:
            msgids = extracted_msgids(path)
        except (OSError, SyntaxError, UnicodeError) as error:
            errors.append(f"{relative}: cannot scan translatable strings: {error}")
            continue
        for msgid in msgids:
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
            try:
                source_placeholders = placeholders(msgid)
                translated_placeholders = placeholders(entry.msgstr)
            except ValueError as error:
                errors.append(
                    f"{catalog_path}:{entry.line}: malformed placeholders: {error}"
                )
            else:
                if source_placeholders != translated_placeholders:
                    errors.append(
                        f"{catalog_path}:{entry.line}: "
                        f"placeholders differ for {msgid!r}"
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
    python_mapping = _error_mapping(PY_ERRORS)
    windows_ui_mapping = _error_mapping(WINDOWS_UI_ERRORS)
    if not codes:
        errors.append(f"{PROTOCOL}: cannot read the ErrorCode constants")
    if set(python_mapping) != set(codes):
        errors.append(
            "session/errors.py mapping does not cover exactly protocol.ErrorCode: "
            f"missing={sorted(set(codes) - set(python_mapping))} "
            f"unknown={sorted(set(python_mapping) - set(codes))}"
        )
    if set(windows_ui_mapping) != set(codes):
        errors.append(
            "windows/ui/pages.py mapping does not cover exactly protocol.ErrorCode: "
            f"missing={sorted(set(codes) - set(windows_ui_mapping))} "
            f"unknown={sorted(set(windows_ui_mapping) - set(codes))}"
        )
    if python_mapping != windows_ui_mapping:
        errors.append(
            "Linux session and Windows UI error mappings use different source msgids: "
            f"python_only={sorted(set(python_mapping.items()) - set(windows_ui_mapping.items()))} "
            f"windows_only={sorted(set(windows_ui_mapping.items()) - set(python_mapping.items()))}"
        )

    # 5. platform metadata ------------------------------------------------
    _check_desktop_metadata(errors)
    _check_installer_metadata(errors)
    _check_polkit_metadata(errors)

    # 6. report ------------------------------------------------------------
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
