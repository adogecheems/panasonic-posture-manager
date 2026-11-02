"""Read-only hardware probe.

The probe only reads sysfs and JSON profile files: it never opens
``/dev/input`` nodes, never grabs devices and never writes to the kernel.  It
can therefore run as an unprivileged user and is safe to attach to a hardware
adaptation report.  Output includes the DMI identities, the resolved profile,
every input device with parsed capabilities and the IIO accelerometers.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import iio, profile, sysfs

PROBE_VERSION = 1


def probe(
    *,
    dmi: Sequence[str] | None = None,
    profile_dirs: Sequence[str | Path] | None = None,
    sysfs_root: str | Path | None = None,
    iio_root: str | Path | None = None,
) -> dict[str, object]:
    """Collect a read-only hardware report."""
    vendor, product = (str(dmi[0]), str(dmi[1])) if dmi is not None else profile.dmi()
    resolved = profile.load_profile((vendor, product), profile_dirs)

    input_devices = sysfs.input_devices(sysfs_root)
    iio_devices = iio.iio_devices(iio_root)

    return {
        "version": PROBE_VERSION,
        "read_only": True,
        "dmi": {"vendor": vendor, "product": product},
        "profile": {
            "id": resolved.id,
            "vendor": resolved.vendor,
            "product": resolved.product,
            "panasonic": resolved.panasonic,
            "switch_name": resolved.switch_name,
            "keyboard_name": resolved.keyboard_name,
            "touchpad_name": resolved.touchpad_name,
            "accel_name": resolved.accel_name,
            "mount_matrix": list(resolved.mount_matrix or []),
            "search_order": [str(directory) for directory in
                             (profile_dirs or profile.PROFILE_DIRS)],
        },
        "input": input_devices,
        "iio": iio_devices,
        "summary": _summary(input_devices, iio_devices),
    }


def _summary(
    input_devices: Sequence[Mapping[str, object]],
    iio_devices: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Human-readable booleans so a report can be skimmed without parsing hex.

    Candidate counts exclude the relay's own virtual switch: while the relay
    runs the probe must not count the program's output as a hardware switch.
    """
    real_devices = [
        entry for entry in input_devices if not entry.get("virtual")
    ]

    def feature(entry: Mapping[str, object], name: str) -> bool:
        features = entry.get("features")
        if not isinstance(features, Mapping):
            return False
        return bool(features.get(name))

    return {
        "input_count": len(input_devices),
        "tablet_switch_candidates": sum(
            1 for entry in real_devices if feature(entry, "switch_only")
        ),
        "internal_keyboard_candidates": sum(
            1
            for entry in real_devices
            if feature(entry, "keyboard_keys")
            and not feature(entry, "external_bus")
        ),
        "internal_touchpad_candidates": sum(
            1
            for entry in real_devices
            if feature(entry, "multitouch")
            and not feature(entry, "direct")
            and not feature(entry, "external_bus")
        ),
        "iio_count": len(iio_devices),
    }


_USAGE = "usage: panasonic-posture-manager-probe [--probe]"


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments in (["--help"], ["-h"]):
        print(_USAGE)
        return 0
    if arguments and arguments != ["--probe"]:
        print(_USAGE, file=sys.stderr)
        return 2
    print(json.dumps(probe(), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
