"""Linux machine-profile lookup and compatibility exports.

The profile schema and matcher are shared with Windows in
``panasonic_posture.machine_profile``.  This adapter supplies Linux DMI
identity and the user, checkout and installed-data lookup order.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Sequence

from ...machine_profile import (
    DEFAULT_ACCEL_NAME as DEFAULT_ACCEL_NAME,
    MOUNT_TOLERANCE as MOUNT_TOLERANCE,
    Profile as Profile,
    apply_mount as apply_mount,
    load_profile as _load_profile,
    parse_mount_matrix as parse_mount_matrix,
)
from ..contracts import CONFIG_DIR, DATA_DIR

_LOG = logging.getLogger(__name__)

DMI_ROOT = Path("/sys/class/dmi/id")
CONFIG_PROFILE_DIR = Path(CONFIG_DIR) / "profiles"
DATA_PROFILE_DIR = Path(DATA_DIR) / "profiles"
SOURCE_PROFILE_DIR = Path(__file__).resolve().parents[4] / "data" / "profiles"

#: Lookup order: user override, development checkout, installed data.  The
#: checkout entry is absent in an installed wheel, so packaged runs still use
#: the data files shipped by the package.  A user override remains authoritative.
PROFILE_DIRS = (CONFIG_PROFILE_DIR, SOURCE_PROFILE_DIR, DATA_PROFILE_DIR)


def _read_text(path: Path) -> str:
    """Read a DMI file; never crash on undecodable bytes or I/O."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def dmi() -> tuple[str, str]:
    """Return ``(sys_vendor, product_name)``, empty when unreadable."""
    return (
        _read_text(DMI_ROOT / "sys_vendor").strip(),
        _read_text(DMI_ROOT / "product_name").strip(),
    )


def dmi_values() -> tuple[str, str]:
    """Alias kept for callers that read DMI separately from a profile load."""
    return dmi()


def load_profile(
    dmi: Sequence[str] | None = None,
    dirs: Iterable[str | Path] | str | Path | None = None,
) -> Profile:
    """Load the best matching Linux profile or the generic fallback."""
    if dmi is None:
        vendor, product = dmi_values()
    else:
        if isinstance(dmi, (str, bytes)):
            raise ValueError("dmi must be a (vendor, product) pair")
        try:
            vendor_value, product_value = dmi
        except (TypeError, ValueError) as exc:
            raise ValueError("dmi must be a (vendor, product) pair") from exc
        vendor = str(vendor_value or "")
        product = str(product_value or "")
    return _load_profile(
        (vendor, product),
        PROFILE_DIRS if dirs is None else dirs,
        log=_LOG,
    )


def profile_directories() -> tuple[Path, ...]:
    """Return the Linux profile lookup order."""
    return PROFILE_DIRS


__all__ = [
    "DEFAULT_ACCEL_NAME",
    "DMI_ROOT",
    "MOUNT_TOLERANCE",
    "PROFILE_DIRS",
    "Profile",
    "apply_mount",
    "dmi",
    "dmi_values",
    "load_profile",
    "parse_mount_matrix",
    "profile_directories",
]
