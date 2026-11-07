"""Manage USB remote wake only on a uniquely identified fixed touch panel."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from ..hardware.sysfs import (
    ABS_MT_POSITION_X, BUS_USB, INPUT_PROP_DIRECT, KEY_A,
    input_device_info, read_bitmap,
)

INPUTS = Path("/sys/class/input")
BTN_TOUCH = 330
MT_AXES = (1 << ABS_MT_POSITION_X) | (1 << (ABS_MT_POSITION_X + 1))
CLAIM = Path("/run/panasonic-posture-manager/touch-wake-owner")


def _panel_wakeup(inputs: Path) -> Path | None:
    candidates = []
    for event in inputs.glob("event*"):
        if not read_bitmap((event / "device/properties").read_text()) & (1 << INPUT_PROP_DIRECT):
            continue
        info = input_device_info(event)
        if not info or "properties_error" in info or "capabilities_error" in info:
            return None
        if (info["virtual"] or not info["features"]["direct"]
                or not info["features"]["event_key"] or not info["features"]["event_abs"]
                or not info["keys"] & (1 << BTN_TOUCH)
                or info["keys"] & (1 << KEY_A)
                or info["abs"] & MT_AXES != MT_AXES):
            continue
        candidates.append((event, info))
    if len(candidates) != 1:
        return None
    event, info = candidates[0]
    if info["bustype_value"] != BUS_USB:
        return None
    # An external USB hub may report its own downstream port as fixed.
    # Require a fixed chain to the root hub, whose devpath is always zero.
    device = None
    for parent in (event / "device").resolve(strict=True).parents:
        if not (parent / "idVendor").is_file() or not (parent / "idProduct").is_file():
            continue
        if (parent / "devpath").read_text().strip() == "0":
            return device / "power/wakeup" if device is not None else None
        if (parent / "removable").read_text().strip() != "fixed":
            return None
        if device is None:
            device = parent
    return None


def _usb_identity(wakeup: Path) -> list[str]:
    # A kernel inode and port name can both be reused after a disconnect.
    device = wakeup.parent.parent
    return [(device / name).read_text().strip()
            for name in ("busnum", "devnum", "idVendor", "idProduct")]


def _wakeup_path(inputs: Path) -> tuple[Path, ...]:
    panel = _panel_wakeup(inputs)
    if panel is None:
        return ()
    nodes = []
    for device in (panel.parent.parent, *panel.parent.parent.parents):
        if (device / "idVendor").is_file() and (device / "idProduct").is_file():
            nodes.append(device / "power/wakeup")
            if (device / "devpath").read_text().strip() == "0":
                controller = device.parent
                # The root hub is shared. Only its actual PCI USB controller
                # belongs to this path, never a neighbouring bus or device.
                if int((controller / "class").read_text(), 16) >> 8 != 0x0c03:
                    return ()
                nodes.append(controller / "power/wakeup")
                return tuple(nodes)
    return ()


class SuspendTouchWake:
    """A boot-local claim allows service restarts to undo only our own write."""

    def __init__(self, inputs: Path = INPUTS, claim: Path = CLAIM,
                 devices: Path = Path("/sys/devices")):
        self.inputs = inputs
        self.claim = claim
        self.devices = devices
        self.active = False

    def _restore(self, desired: Path | None) -> None:
        try:
            path, inode, identity = json.loads(self.claim.read_text())
            previous = Path(path)
            if (type(inode) is not int or previous.parts[-2:] != ("power", "wakeup")
                    or not previous.is_relative_to(self.devices)
                    or previous.resolve() != previous or not isinstance(identity, list)):
                raise ValueError("invalid touchscreen wake ownership record")
        except FileNotFoundError:
            return
        except (ValueError, TypeError) as error:
            self.claim.rename(self.claim.with_name(f"{self.claim.name}.corrupt-{time.time_ns()}"))
            logging.getLogger(__name__).warning("discarding corrupt wake ownership record: %s", error)
            return
        try:
            with previous.open("r+") as node:
                if os.fstat(node.fileno()).st_ino == inode and _usb_identity(previous) == identity:
                    if previous == desired:
                        return
                    if node.read().strip() == "enabled":
                        node.seek(0)
                        node.write("disabled\n")
        except FileNotFoundError:
            pass  # sysfs removed this incarnation of the device
        self.claim.unlink(missing_ok=True)

    def sync(self, enabled: bool) -> bool:
        self.active = True
        try:
            path = _wakeup_path(self.inputs)
            # Enabling a shared hub can cause unsolicited system resumes.
            # The platform owns shared upstream switches. Require its wake
            # policy to be ready before arming this panel.
            candidate = path[0] if path and all(
                node.read_text().strip() == "enabled" for node in path[1:]
            ) else None
        except (OSError, ValueError):
            candidate = None  # incomplete discovery cannot authorize a wake source
        self._restore(candidate if enabled else None)
        if candidate is None or not candidate.is_relative_to(self.devices):
            return False
        try:
            with candidate.open("r+" if enabled else "r") as node:
                current = node.read().strip()
                if current not in ("enabled", "disabled"):
                    return False
                if enabled and current == "disabled":
                    identity = _usb_identity(candidate)
                    # Revalidate the discovery after opening the node. Hotplug
                    # must not redirect a privileged write to a replacement.
                    if (_panel_wakeup(self.inputs) != candidate
                            or candidate.stat().st_ino != os.fstat(node.fileno()).st_ino
                            or _usb_identity(candidate) != identity):
                        return False
                    self.claim.parent.mkdir(mode=0o700, exist_ok=True)
                    temporary = self.claim.with_suffix(".tmp")
                    temporary.write_text(json.dumps([str(candidate), os.fstat(node.fileno()).st_ino, identity]))
                    temporary.replace(self.claim)
                    node.seek(0)
                    node.write("enabled\n")
                    node.flush()
                    node.seek(0)
                    if node.read().strip() != "enabled":
                        raise OSError("touchscreen wakeup setting did not take effect")
        except FileNotFoundError:
            return False
        return True

    def release(self) -> None:
        if self.active:
            self._restore(None)
            self.active = False
