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
        self.controller = self.base / "controller"
        self.controller.mkdir()
        for name, value in (("vendor", "0x8086"), ("device", "0x9ded"), ("class", "0x0c0330")):
            (self.controller / name).write_text(value)
        (self.controller / "power").mkdir()
        self.controller_wake = self.controller / "power/wakeup"
        self.controller_wake.write_text("enabled\n")
        self.root_hub = self.controller / "usb-root"
        self.root_hub.mkdir()
        for name, value in (("idVendor", "1d6b"), ("idProduct", "0002"), ("devpath", "0"), ("busnum", "1"), ("devnum", "1")):
            (self.root_hub / name).write_text(value)
        (self.root_hub / "power").mkdir()
        self.hub_wake = self.root_hub / "power/wakeup"
        self.hub_wake.write_text("enabled\n")
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

    def test_disabled_upstream_prevents_arming_without_modifying_shared_nodes(self):
        node = self.panel()
        for blocked in (self.hub_wake, self.controller_wake):
            with self.subTest(blocked=blocked):
                blocked.write_text("disabled\n")
                self.assertFalse(self.wake.sync(True))
                self.assertEqual(node.read_text().strip(), "disabled")
                self.assertEqual(blocked.read_text().strip(), "disabled")
                blocked.write_text("enabled\n")

    def test_upstream_revocation_releases_only_our_panel_setting(self):
        node = self.panel()
        self.assertTrue(self.wake.sync(True))
        self.hub_wake.write_text("disabled\n")
        self.assertFalse(self.wake.sync(True))
        self.assertEqual(node.read_text().strip(), "disabled")
        self.assertEqual(self.hub_wake.read_text().strip(), "disabled")

    def test_missing_upstream_wakeup_is_not_reported_as_supported(self):
        node = self.panel()
        self.hub_wake.unlink()
        self.assertFalse(self.wake.sync(True))
        self.assertEqual(node.read_text().strip(), "disabled")

    def test_legacy_single_panel_claim_keeps_original_restore_value(self):
        import json
        from panasonic_posture.service.suspend_touch import _usb_identity
        node = self.panel()
        self.claim.parent.mkdir()
        self.claim.write_text(json.dumps([str(node), node.stat().st_ino, _usb_identity(node)]))
        node.write_text("enabled\n")
        self.wake.sync(True)
        self.wake.release()
        self.assertEqual(node.read_text().strip(), "disabled")
        self.assertEqual(self.hub_wake.read_text().strip(), "enabled")
        self.assertEqual(self.controller_wake.read_text().strip(), "enabled")

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
