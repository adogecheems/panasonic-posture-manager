"""Platform-neutral machine profile schema and lookup.

A profile is a small JSON document that disambiguates device names and sensor
orientation for one machine.  Discovery stays capability based: a profile only
narrows ambiguous candidates, it never widens the security boundary (the relay
still verifies capabilities on the opened device).

Platform adapters provide the machine identity and ordered profile directories.
The first matching profile wins.  This module contains no default filesystem
location and no platform identity probe.
"""

from __future__ import annotations

import fnmatch
import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable, Sequence

from .contracts import SUPPORT_TIERS
from .mounting import (
    MOUNT_TOLERANCE as MOUNT_TOLERANCE,
    parse_mount_matrix as parse_mount_matrix,
    apply_mount as apply_mount,
)

_LOG = logging.getLogger(__name__)

PROFILE_VERSION = 1
DEFAULT_ACCEL_NAME = "accel_3d"


class _Match(Enum):
    INVALID = 0
    EMPTY = 1
    NO = 2
    YES = 3


class _InvalidProfile(ValueError):
    """A profile file is present but does not follow the schema."""


@dataclass(frozen=True)
class Profile:
    """Resolved profile for the current machine."""

    id: str = "generic"
    vendor: str = ""
    product: str = ""
    switch_name: str | None = None
    keyboard_name: str | None = None
    touchpad_name: str | None = None
    accel_name: str = DEFAULT_ACCEL_NAME
    mount_matrix: tuple[float, ...] | None = None
    support_tier: str = "compatible"

    @property
    def supported(self) -> bool:
        return self.support_tier in ("validated", "compatible")

    @property
    def label(self) -> str:
        return self.product or self.id


def _optional_name(data: dict, key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise _InvalidProfile(f"{key!r} must be a string or null")
    name = value.strip()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        # NUL/control characters cannot be passed to execve/Popen and would
        # only show up as an opaque ValueError in the privileged executor.
        raise _InvalidProfile(f"{key!r} contains control characters")
    return name or None


def _parse_support_tier(value: object) -> str:
    if value is None:
        return "compatible"
    if not isinstance(value, str) or value not in SUPPORT_TIERS:
        raise _InvalidProfile(
            "support_tier must be one of " + ", ".join(SUPPORT_TIERS)
        )
    return value


def _parse_product_globs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        raise _InvalidProfile("match.product_glob must be a string or list of strings")
    patterns: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise _InvalidProfile("match.product_glob entries must be strings")
        pattern = item.strip()
        if not pattern:
            raise _InvalidProfile("match.product_glob entries must not be empty")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in pattern):
            raise _InvalidProfile("match.product_glob contains control characters")
        patterns.append(pattern.casefold())
    return tuple(patterns)


def _match_profile(match: object, vendor: str, product: str) -> _Match:
    """Classify the profile ``match`` block against the current DMI values."""
    if match is None:
        return _Match.EMPTY
    if isinstance(match, str):
        if not match.strip():
            return _Match.EMPTY
        return _Match.YES if match.strip() == product else _Match.NO
    if not isinstance(match, dict):
        raise _InvalidProfile("match must be an object or a product string")

    vendor_filter = match.get("vendor")
    product_filter = match.get("product")
    product_globs = _parse_product_globs(match.get("product_glob"))
    if vendor_filter is not None and not isinstance(vendor_filter, str):
        raise _InvalidProfile("match.vendor must be a string")
    if product_filter is not None and not isinstance(product_filter, str):
        raise _InvalidProfile("match.product must be a string")
    for key in match:
        if key not in ("vendor", "product", "product_glob"):
            _LOG.debug("ignoring unknown profile match key %r", key)

    vendor_filter = (vendor_filter or "").strip() or None
    product_filter = (product_filter or "").strip() or None
    if vendor_filter is None and product_filter is None and not product_globs:
        return _Match.EMPTY
    if vendor_filter is not None and vendor_filter != vendor:
        return _Match.NO
    if product_filter is not None and product_filter != product:
        return _Match.NO
    if product_globs and not any(
        fnmatch.fnmatchcase(product.casefold(), pattern) for pattern in product_globs
    ):
        return _Match.NO
    return _Match.YES


def _parse_profile(
    path: Path,
    vendor: str,
    product: str,
    log: logging.Logger,
) -> Profile | None:
    """Load one profile file; log and skip anything invalid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        log.warning("skipping profile %s: cannot read: %s", path, exc)
        return None
    except ValueError as exc:
        log.warning("skipping profile %s: invalid JSON: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("skipping profile %s: top-level value must be an object", path)
        return None

    version = data.get("version", PROFILE_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) \
            or version != PROFILE_VERSION:
        log.warning(
            "skipping profile %s: unsupported version %r", path, version
        )
        return None

    identifier = data.get("id", path.stem)
    if not isinstance(identifier, str) or not identifier.strip():
        log.warning("skipping profile %s: id must be a non-empty string", path)
        return None

    try:
        match = _match_profile(data.get("match"), vendor, product)
        if match is _Match.INVALID:
            raise _InvalidProfile("invalid match")
        if match is _Match.EMPTY:
            # Explicitly documented semantics: an empty match matches *no*
            # machine.  This prevents a broad/blank profile from shadowing the
            # built-in profiles or the generic fallback.
            log.warning(
                "profile %s has an empty match; it matches no machine", path
            )
            return None
        if match is _Match.NO:
            return None

        return Profile(
            id=identifier.strip(),
            vendor=vendor,
            product=product,
            switch_name=_optional_name(data, "switch_name"),
            keyboard_name=_optional_name(data, "keyboard_name"),
            touchpad_name=_optional_name(data, "touchpad_name"),
            accel_name=_optional_name(data, "accel_name") or DEFAULT_ACCEL_NAME,
            mount_matrix=parse_mount_matrix(data.get("mount_matrix")),
            support_tier=_parse_support_tier(data.get("support_tier")),
        )
    except ValueError as exc:  # includes _InvalidProfile and mount errors
        log.warning("skipping profile %s: %s", path, exc)
        return None


def _as_directories(
    dirs: Iterable[str | Path] | str | Path,
) -> tuple[Path, ...]:
    if isinstance(dirs, (str, Path)):
        return (Path(dirs),)
    return tuple(Path(directory) for directory in dirs)


def load_profile(
    dmi: Sequence[str],
    dirs: Iterable[str | Path] | str | Path,
    *,
    log: logging.Logger | None = None,
) -> Profile:
    """Load the best matching profile for an explicit identity and search path.

    Never raises for malformed profile files: each one is logged and skipped.
    Falls back to a generic :class:`Profile` with the DMI identities.
    """
    if isinstance(dmi, (str, bytes)):
        raise ValueError("dmi must be a (vendor, product) pair")
    try:
        vendor_value, product_value = dmi
    except (TypeError, ValueError) as exc:
        raise ValueError("dmi must be a (vendor, product) pair") from exc
    vendor = str(vendor_value or "")
    product = str(product_value or "")
    log = log or _LOG

    for directory in _as_directories(dirs):
        try:
            candidates = sorted(Path(directory).glob("*.json"))
        except OSError as exc:
            _LOG.debug("cannot scan profile directory %s: %s", directory, exc)
            continue
        for path in candidates:
            found = _parse_profile(path, vendor, product, log)
            if found is not None:
                _LOG.debug("profile %s matched through %s", found.id, path)
                return found
    return Profile(vendor=vendor, product=product)
