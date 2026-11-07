"""Suspend wake policy uses a private sysfs tree and never opens input nodes."""
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.service.suspend_touch import SuspendTouchWake


class SuspendTouchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root_hub = self.base / "usb-root"
        self.root_hub.mkdir()
        for name, value in (("idVendor", "1d6b"), ("idProduct", "0002"), ("devpath", "0")):
            (self.root_hub / name).write_text(value)
        self.inputs = self.base / "input"
        self.claim = self.base / "runtime" / "touch-owner"
        self.inputs.mkdir()
        self.wake = SuspendTouchWake(self.inputs, self.claim, self.base)

    def panel(self, number=1, removable="fixed", initial="disabled"):
        usb = self.root_hub / f"usb{number}"
        dev = usb / "iface" / "input"
        event = self.inputs / f"event{number}"
        event.mkdir()
        dev.mkdir(parents=True)
        (event / "device").symlink_to(dev)
        for path, value in {
            "name": "internal touch", "properties": "2", "id/bustype": "0003",
            "capabilities/abs": "60000000000000", "capabilities/key": "400 0 0 0 0 0",
            "capabilities/ev": "b",
        }.items():
            (dev / path).parent.mkdir(exist_ok=True)
            (dev / path).write_text(value)
        for name, value in (("devpath", str(number)), ("busnum", "1"), ("devnum", str(number)), ("removable", removable), ("idVendor", "0eef"), ("idProduct", "c300")):
            (usb / name).write_text(value)
        (usb / "power").mkdir()
        wake = usb / "power/wakeup"
        wake.write_text(initial + "\n")
        return wake

    def test_opt_in_restores_original_across_restart(self):
        node = self.panel()
        self.assertTrue(self.wake.sync(False))
        self.assertEqual(node.read_text().strip(), "disabled")
        self.assertTrue(self.wake.sync(True))
        self.assertEqual(node.read_text().strip(), "enabled")
        SuspendTouchWake(self.inputs, self.claim, self.base).sync(False)
        self.assertEqual(node.read_text().strip(), "disabled")

    def test_native_enabled_is_preserved(self):
        node = self.panel(initial="enabled")
        self.wake.sync(True)
        self.wake.sync(False)
        self.assertEqual(node.read_text().strip(), "enabled")

    def test_ambiguity_releases_owned_device(self):
        original = self.panel()
        self.wake.sync(True)
        second = self.panel(2)
        self.assertFalse(self.wake.sync(True))
        self.assertEqual(original.read_text().strip(), "disabled")
        self.assertEqual(second.read_text().strip(), "disabled")

    def test_replacement_on_same_port_does_not_inherit_restore(self):
        node = self.panel()
        self.wake.sync(True)
        (node.parent.parent / "devnum").write_text("88")
        self.wake.sync(False)
        self.assertEqual(node.read_text().strip(), "enabled")

    def test_unstarted_duplicate_cannot_release_owners_policy(self):
        node = self.panel()
        self.wake.sync(True)
        SuspendTouchWake(self.inputs, self.claim, self.base).release()
        self.assertEqual(node.read_text().strip(), "enabled")
        self.wake.release()
        self.assertEqual(node.read_text().strip(), "disabled")

    def test_external_or_unknown_port_rejected(self):
        node = self.panel(removable="unknown")
        self.assertFalse(self.wake.sync(True))
        self.assertEqual(node.read_text().strip(), "disabled")

    def test_fixed_panel_on_external_hub_is_rejected(self):
        node = self.panel()
        (self.root_hub / "devpath").write_text("2")
        (self.root_hub / "removable").write_text("removable")
        self.assertFalse(self.wake.sync(True))
        self.assertEqual(node.read_text().strip(), "disabled")

    def test_no_device_and_missing_wake_attribute(self):
        self.assertFalse(self.wake.sync(True))
        node = self.panel()
        node.unlink()
        self.assertFalse(self.wake.sync(True))
        self.assertFalse(node.exists())
