"""Windows model profiles with capability-based fallback for other machines."""
from collections.abc import Iterable
from dataclasses import dataclass
import logging
from pathlib import Path

from panasonic_posture.machine_profile import load_profile
from panasonic_posture.windows.hardware.machine_identity import read_machine_identity
from panasonic_posture.windows.hardware.sensor import IDENTITY

log = logging.getLogger(__name__)

# Profiles are packaged beside the application. They select explicit device
# and orientation knowledge; they do not grant access to hardware.
WINDOWS_PROFILE_DIR = Path(__file__).resolve().parents[3] / 'data' / 'profiles'


@dataclass(frozen=True)
class WindowsProfile:
    id: str
    vendor: str
    product: str
    support_tier: str
    mount_matrix: tuple[float, ...]


def resolve_windows_profile(
    identity: tuple[str, str] | None = None,
    *,
    profile_dirs: Iterable[str | Path] | None = None,
) -> WindowsProfile:
    """Resolve a known product family or return a generic capability profile.

    Unknown machines are never rejected here. The Windows sensor, ETW source,
    optional slate output and display session probe their own capabilities at
    runtime, matching the Linux generic-profile path. A profile only narrows
    known behavior such as the verified QV product families.
    """
    if identity is None:
        try:
            identity = read_machine_identity()
        except OSError as error:
            log.warning('cannot read Windows BIOS identity; using generic profile: %s', error)
            identity = ('', '')
    try:
        vendor_value, product_value = identity
    except (TypeError, ValueError) as error:
        raise ValueError('Windows machine identity must be a (vendor, product) pair') from error
    vendor, product = str(vendor_value or '').strip(), str(product_value or '').strip()
    directories = (WINDOWS_PROFILE_DIR,) if profile_dirs is None else profile_dirs
    resolved = load_profile((vendor, product), directories)
    return WindowsProfile(
        id=resolved.id,
        vendor=resolved.vendor,
        product=resolved.product,
        support_tier=resolved.support_tier,
        mount_matrix=resolved.mount_matrix or IDENTITY,
    )


__all__ = ['WINDOWS_PROFILE_DIR', 'WindowsProfile', 'resolve_windows_profile']
