"""Tests for panasonic_posture.hardware.sysfs.

Capability bitmap fixtures are copied from a real CF-QV8 kernel (space
separated 64-bit words, most significant word first) so the parser is tested
against the kernel format rather than an invented one.
"""

import errno
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from panasonic_posture.hardware import sysfs
from panasonic_posture.model import HardwareStatus
from panasonic_posture.protocol import RELAY_VIRTUAL_NAME, RELAY_VIRTUAL_PHYS

# Real `capabilities/key` fixtures.
KEYBOARD_KEY_BITMAP = (
    "402000007 ff803078f800d001 feffffdfffcfffff fffffffffffffffe"
)
POWER_BUTTON_KEY_BITMAP = "8000 10000000000000 0"

CAPABILITY_NAMES = ("ev", "key", "rel", "abs", "msc", "sw", "led", "snd", "ff")


def write_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")


def make_input(root, event, name, *, phys="", capabilities=None, properties="0",
               bustype="0019", vendor="0000", product="0000", raw_name=None):
    device = Path(root) / event / "device"
    device.mkdir(parents=True, exist_ok=True)
    if raw_name is not None:
        (device / "name").write_bytes(raw_name)
    else:
        write_text(device / "name", name)
    write_text(device / "phys", phys)
    write_text(device / "id" / "bustype", bustype)
    write_text(device / "id" / "vendor", vendor)
    write_text(device / "id" / "product", product)
    write_text(device / "properties", properties)
    values = {
        "ev": "21",
        "key": "0",
        "rel": "0",
        "abs": "0",
        "msc": "0",
        "sw": "0",
        "led": "0",
        "snd": "0",
        "ff": "0",
    }
    values.update(capabilities or {})
    for capability, value in values.items():
        if value is None:
            continue
        write_text(device / "capabilities" / capability, value)
    return device


def switch_capabilities(**overrides):
    values = {"ev": "21", "key": "0", "sw": "2"}
    values.update(overrides)
    return values


class FakeDevices:
    """Fake /dev/input opener + ioctl implementation for query_switch tests."""

    def __init__(self, devices):
        self.devices = devices
        self.opened = {}
        self.next_fd = 9000

    def open(self, path):
        event = Path(path).name
        entry = self.devices.get(event)
        if entry is None:
            raise FileNotFoundError(path)
        failure = entry.get("open_error")
        if failure is not None:
            raise failure
        descriptor = self.next_fd
        self.next_fd += 1
        self.opened[descriptor] = dict(entry, event=event)
        return descriptor

    def ioctl(self, descriptor, request, buffer, mutate):
        entry = self.opened[descriptor]
        if request == sysfs.EVIOCGNAME:
            raw = entry["name"].encode("utf-8") + b"\0"
            buffer[: len(raw)] = raw
            return 0
        if request == sysfs.EVIOCGPHYS:
            failure = entry.get("phys_error")
            if failure is not None:
                raise failure
            raw = entry.get("phys", "").encode("utf-8") + b"\0"
            buffer[: len(raw)] = raw
            return 0
        if request == sysfs.EVIOCGSW:
            failure = entry.get("state_error")
            if failure is not None:
                raise failure
            if entry.get("state"):
                buffer[0] |= 1 << sysfs.SW_TABLET_MODE
            return 0
        raise AssertionError(f"unexpected ioctl request {request:#x}")


class BitmapTests(unittest.TestCase):
    def test_platform_word_width_matches_pointer_size(self):
        self.assertEqual(sysfs.WORD_BITS, struct.calcsize("P") * 8)
        self.assertEqual(sysfs.WORD_BYTES, struct.calcsize("P"))

    def test_real_keyboard_bitmap(self):
        value = sysfs.read_bitmap(KEYBOARD_KEY_BITMAP)
        expected = (
            (0x402000007 << 192)
            | (0xFF803078F800D001 << 128)
            | (0xFEFFFFDFFFCFFFFF << 64)
            | 0xFFFFFFFFFFFFFFFE
        )
        self.assertEqual(value, expected)
        self.assertTrue(value & (1 << sysfs.KEY_A))
        self.assertTrue(value & (1 << sysfs.KEY_Z))
        self.assertFalse(value & 1)  # KEY_RESERVED is never set

    def test_msw_is_the_first_word(self):
        self.assertEqual(sysfs.read_bitmap("1 0"), 1 << sysfs.WORD_BITS)
        self.assertEqual(
            sysfs.read_bitmap(POWER_BUTTON_KEY_BITMAP),
            (0x8000 << 128) | (0x10000000000000 << 64),
        )

    def test_32_bit_words_can_be_simulated(self):
        self.assertEqual(sysfs.read_bitmap("2 1", word_bits=32), (2 << 32) | 1)
        self.assertEqual(sysfs.read_bitmap("0", word_bits=32), 0)

    def test_leading_zero_words_are_trimmed_by_the_kernel(self):
        # The kernel omits high all-zero words; both forms must parse equal.
        trimmed = sysfs.read_bitmap("1")
        padded = sysfs.read_bitmap("0 0 1")
        self.assertEqual(trimmed, padded)

    def test_hex_uppercase_and_leading_zeros(self):
        self.assertEqual(sysfs.read_bitmap("FE"), 0xFE)
        self.assertEqual(sysfs.read_bitmap("0000000000000001"), 1)

    def test_empty_bitmap_is_invalid(self):
        for text in ("", "   ", "\n"):
            with self.assertRaises(ValueError, msg=repr(text)):
                sysfs.read_bitmap(text)

    def test_invalid_words_are_rejected(self):
        for text in ("zz", "12gg", "-1", "1 2 3 4 x"):
            with self.assertRaises(ValueError, msg=repr(text)):
                sysfs.read_bitmap(text)

    def test_word_too_wide_is_rejected(self):
        with self.assertRaises(ValueError):
            sysfs.read_bitmap("1" * (sysfs.WORD_BITS // 4 + 1))

    def test_invalid_word_width_is_rejected(self):
        with self.assertRaises(ValueError):
            sysfs.read_bitmap("1", word_bits=0)
        with self.assertRaises(ValueError):
            sysfs.read_bitmap("1", word_bits=63)


class CapabilityTests(unittest.TestCase):
    def test_read_capabilities_from_device_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_input(tmp, "event0", "device", capabilities={
                "key": KEYBOARD_KEY_BITMAP,
                "sw": "2",
            })
            capabilities = sysfs.read_capabilities(device)
        self.assertEqual(capabilities["sw"], 1 << sysfs.SW_TABLET_MODE)
        self.assertEqual(
            capabilities["key"] & (1 << sysfs.KEY_A), 1 << sysfs.KEY_A
        )

    def test_read_capabilities_from_event_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "device", capabilities={"sw": "3"})
            capabilities = sysfs.read_capabilities(Path(tmp) / "event0")
        self.assertEqual(capabilities["sw"], 3)

    def test_missing_capability_file_is_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "device", capabilities={"key": None})
            capabilities = sysfs.read_capabilities(Path(tmp) / "event0")
        self.assertNotIn("key", capabilities)

    def test_malformed_capability_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "device", capabilities={"key": "nope"})
            with self.assertRaises(ValueError):
                sysfs.read_capabilities(Path(tmp) / "event0")

    def test_missing_capabilities_directory_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OSError):
                sysfs.read_capabilities(tmp)


class InputDevicesTests(unittest.TestCase):
    def test_sysfs_listing_parses_features(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(
                tmp,
                "event2",
                "AT Translated Set 2 keyboard",
                phys="isa0060/serio0/input0",
                capabilities={"ev": "120013", "key": KEYBOARD_KEY_BITMAP},
            )
            make_input(
                tmp,
                "event9",
                "Synaptics TM3562-001",
                phys="i2c-SYNA8001:00",
                capabilities={"ev": "b", "abs": "20000000000000",
                              "key": "e520 30000 0 0 0 0"},
                properties="5",
            )
            make_input(
                tmp,
                "event99",
                RELAY_VIRTUAL_NAME,
                phys=RELAY_VIRTUAL_PHYS,
                capabilities=switch_capabilities(),
            )
            devices = {entry["name"]: entry for entry in sysfs.input_devices(tmp)}
        self.assertIn("AT Translated Set 2 keyboard", devices)
        keyboard = devices["AT Translated Set 2 keyboard"]
        self.assertTrue(keyboard["features"]["keyboard_keys"])
        self.assertFalse(keyboard["features"]["external_bus"])
        self.assertFalse(keyboard["virtual"])
        self.assertIn("key", keyboard["capabilities"])
        self.assertGreater(keyboard["keys"], 0)
        self.assertEqual(keyboard["switches"], 0)
        self.assertEqual(keyboard["props"], 0)
        self.assertTrue(devices[RELAY_VIRTUAL_NAME]["virtual"])
        touchpad = devices["Synaptics TM3562-001"]
        self.assertTrue(touchpad["features"]["multitouch"])
        self.assertFalse(touchpad["features"]["direct"])

    def test_invalid_utf8_device_name_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "x", raw_name=b"Bad\xffName\n")
            devices = sysfs.input_devices(tmp)
        self.assertEqual(len(devices), 1)
        self.assertIn("\ufffd", devices[0]["name"])


class QuerySwitchTests(unittest.TestCase):
    PHYS = "PNP0C50/input0"

    def query(self, sysfs_dir, devices, **kwargs):
        dev_root = Path(sysfs_dir) / "dev"
        dev_root.mkdir(exist_ok=True)
        fake = FakeDevices(devices)
        return sysfs.query_switch(
            kwargs.pop("switch_name", None),
            sysfs_root=sysfs_dir,
            dev_root=dev_root,
            open_device=fake.open,
            ioctl_device=fake.ioctl,
            **kwargs,
        )

    def test_absent_when_no_input_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, status = self.query(tmp, {})
        self.assertFalse(state)
        self.assertIs(status, HardwareStatus.ABSENT)

    def test_ok_reads_switch_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches", phys=self.PHYS,
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches",
                                 "phys": self.PHYS, "state": True}}
            )
        self.assertIs(state, True)
        self.assertIs(status, HardwareStatus.OK)

    def test_missing_phys_attribute_is_not_a_read_error(self):
        # Intel HID switches has no `phys`; EVIOCGPHYS returns ENOENT.  This
        # must not be mistaken for "the /dev node is missing".
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp,
                {"event0": {"name": "Intel HID switches",
                            "phys_error": FileNotFoundError(
                                errno.ENOENT, "no phys"),
                            "state": False}},
            )
        self.assertIs(state, False)
        self.assertIs(status, HardwareStatus.OK)

    def test_own_virtual_switch_is_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event5", RELAY_VIRTUAL_NAME,
                       phys=RELAY_VIRTUAL_PHYS,
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp, {"event5": {"name": RELAY_VIRTUAL_NAME,
                                 "phys": RELAY_VIRTUAL_PHYS, "state": True}}
            )
        self.assertFalse(state)
        self.assertIs(status, HardwareStatus.ABSENT)

    def test_physical_switch_wins_over_virtual_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches", phys=self.PHYS,
                       capabilities=switch_capabilities())
            make_input(tmp, "event5", RELAY_VIRTUAL_NAME,
                       phys=RELAY_VIRTUAL_PHYS,
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp,
                {
                    "event0": {"name": "Intel HID switches", "phys": self.PHYS,
                               "state": False},
                    "event5": {"name": RELAY_VIRTUAL_NAME,
                               "phys": RELAY_VIRTUAL_PHYS, "state": True},
                },
            )
        self.assertIs(state, False)
        self.assertIs(status, HardwareStatus.OK)

    def test_ambiguous_matches_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "switch a", capabilities=switch_capabilities())
            make_input(tmp, "event1", "switch b", capabilities=switch_capabilities())
            state, status = self.query(
                tmp,
                {
                    "event0": {"name": "switch a", "state": True},
                    "event1": {"name": "switch b", "state": False},
                },
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.AMBIGUOUS)

    def test_switch_name_filters_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities())
            make_input(tmp, "event1", "other tablet switch",
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp,
                {
                    "event0": {"name": "Intel HID switches", "state": True},
                    "event1": {"name": "other tablet switch", "state": False},
                },
                switch_name="Intel HID switches",
            )
        self.assertIs(state, True)
        self.assertIs(status, HardwareStatus.OK)

    def test_permission_error_is_not_reported_as_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp,
                {"event0": {"name": "Intel HID switches",
                            "open_error": PermissionError(errno.EACCES, "denied")}},
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.PERMISSION)

    def test_state_read_error_is_not_reported_as_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp,
                {"event0": {"name": "Intel HID switches",
                            "state_error": OSError(errno.EIO, "io error")}},
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.READ_ERROR)

    def test_malformed_capability_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities(key="not-hex"))
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches", "state": True}}
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.READ_ERROR)

    def test_device_with_keys_is_never_used_as_switch(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities(key="1"))
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches", "state": True}}
            )
        self.assertFalse(state)
        self.assertIs(status, HardwareStatus.ABSENT)

    def test_switch_with_extra_switch_bits_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Lid Switch",
                       capabilities=switch_capabilities(sw="3"))
            state, status = self.query(
                tmp, {"event0": {"name": "Lid Switch", "state": True}}
            )
        self.assertFalse(state)
        self.assertIs(status, HardwareStatus.ABSENT)

    def test_missing_key_capability_fails_closed(self):
        # Without the key bitmap we cannot prove "no EV_KEY": the device claims
        # the tablet switch, so this is a read error rather than "absent".
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities(key=None))
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches", "state": True}}
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.READ_ERROR)

    def test_ev_declaring_key_type_is_rejected(self):
        # backend/switch.c rejects devices that declare EV_KEY events even
        # when the key code bitmap is empty; Python must agree.
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities(ev="22"))
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches", "state": True}}
            )
        self.assertFalse(state)
        self.assertIs(status, HardwareStatus.ABSENT)

    def test_missing_ev_capability_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities(ev=None))
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches", "state": True}}
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.READ_ERROR)

    def test_malformed_ev_capability_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities(ev="not-hex"))
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches", "state": True}}
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.READ_ERROR)

    def test_unreadable_ev_capability_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_input(tmp, "event0", "Intel HID switches",
                                capabilities=switch_capabilities())
            (device / "capabilities" / "ev").chmod(0o000)
            state, status = self.query(
                tmp, {"event0": {"name": "Intel HID switches", "state": True}}
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.PERMISSION)

    def test_identity_mismatch_is_a_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities())
            state, status = self.query(
                tmp, {"event0": {"name": "recycled device", "state": True}}
            )
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.READ_ERROR)

    def test_missing_device_node_is_a_read_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       capabilities=switch_capabilities())
            state, status = self.query(tmp, {})
        self.assertIsNone(state)
        self.assertIs(status, HardwareStatus.READ_ERROR)

    def test_unreadable_sysfs_name_is_treated_as_absent_or_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = Path(tmp) / "event0" / "device"
            event.mkdir(parents=True)
            # No name file at all: the entry is not a candidate.
            state, status = self.query(tmp, {})
        self.assertFalse(state)
        self.assertIn(status, (HardwareStatus.ABSENT, HardwareStatus.READ_ERROR))


class ProbeSummaryTests(unittest.TestCase):
    """The read-only probe must not count the relay's own output as hardware."""

    def test_summary_excludes_the_relays_own_virtual_switch(self):
        from panasonic_posture.hardware import probe as hardware_probe

        with tempfile.TemporaryDirectory() as tmp:
            make_input(tmp, "event0", "Intel HID switches",
                       phys="PNP0C50/input0", capabilities=switch_capabilities())
            make_input(tmp, "event5", RELAY_VIRTUAL_NAME,
                       phys=RELAY_VIRTUAL_PHYS,
                       capabilities=switch_capabilities())
            report = hardware_probe.probe(
                dmi=("Panasonic Corporation", "CFQV8-1L"),
                profile_dirs=[],
                sysfs_root=tmp,
                iio_root=Path(tmp) / "iio",
            )
        self.assertTrue(report["read_only"])
        self.assertEqual(report["summary"]["input_count"], 2)
        # Without the virtual-device filter this would be 2.
        self.assertEqual(report["summary"]["tablet_switch_candidates"], 1)
        self.assertEqual(report["summary"]["iio_count"], 0)


class SwitchReaderCachingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.clock = 100.0

    def add_switch(self, state: bool = False) -> None:
        make_input(
            self.root,
            "event0",
            "Intel HID switches",
            capabilities=switch_capabilities(),
        )
        if not hasattr(self, "devices"):
            self.devices = {}
        self.devices["event0"] = {"name": "Intel HID switches", "state": state}

    def reader(self, rescan_seconds: float = 1.0):
        if not hasattr(self, "devices"):
            self.devices = {}
        fake = FakeDevices(self.devices)
        return sysfs.SwitchReader(
            "Intel HID switches",
            sysfs_root=self.root,
            dev_root=Path("/unused"),
            open_device=fake.open,
            ioctl_device=fake.ioctl,
            rescan_seconds=rescan_seconds,
            monotonic=lambda: self.clock,
        ), fake

    def test_cached_candidate_avoids_full_scan_and_tracks_state(self) -> None:
        self.add_switch(state=False)
        reader, fake = self.reader()
        with mock.patch.object(
            sysfs, "_scan_candidates", wraps=sysfs._scan_candidates
        ) as scan:
            self.assertEqual(reader.read(), (False, HardwareStatus.OK))
            self.assertEqual(reader.candidate, "event0")
            self.devices["event0"]["state"] = True
            self.clock += 0.1
            self.assertEqual(reader.read(), (True, HardwareStatus.OK))
            self.assertEqual(scan.call_count, 1)
            self.clock += 1.0
            self.assertEqual(reader.read(), (True, HardwareStatus.OK))
            self.assertEqual(scan.call_count, 2)
        self.assertGreaterEqual(len(fake.opened), 3)

    def test_absent_state_is_cached_between_rescans(self) -> None:
        reader, _fake = self.reader()
        with mock.patch.object(
            sysfs, "_scan_candidates", wraps=sysfs._scan_candidates
        ) as scan:
            self.assertEqual(reader.read(), (False, HardwareStatus.ABSENT))
            self.clock += 0.9
            self.assertEqual(reader.read(), (False, HardwareStatus.ABSENT))
            self.assertEqual(scan.call_count, 1)
            self.clock += 0.2
            self.assertEqual(reader.read(), (False, HardwareStatus.ABSENT))
            self.assertEqual(scan.call_count, 2)

    def test_hotplug_is_detected_on_the_next_rescan(self) -> None:
        reader, _fake = self.reader()
        self.assertEqual(reader.read(), (False, HardwareStatus.ABSENT))
        self.add_switch(state=True)
        self.clock += 1.1
        self.assertEqual(reader.read(), (True, HardwareStatus.OK))
        self.assertEqual(reader.candidate, "event0")

    def test_failed_cached_read_invalidates_candidate(self) -> None:
        self.add_switch(state=False)
        reader, fake = self.reader()
        self.assertEqual(reader.read(), (False, HardwareStatus.OK))
        fake.devices["event0"]["open_error"] = FileNotFoundError("gone")
        self.clock += 0.1
        self.assertEqual(reader.read(), (None, HardwareStatus.READ_ERROR))
        self.assertIsNone(reader.candidate)
        # The very next poll rediscovers instead of returning stale ON/OFF.
        fake.devices["event0"].pop("open_error")
        self.devices["event0"]["state"] = True
        self.clock += 0.1
        self.assertEqual(reader.read(), (True, HardwareStatus.OK))

    def test_invalidate_forces_rediscovery_after_suspend(self) -> None:
        self.add_switch(state=False)
        reader, _fake = self.reader(rescan_seconds=10.0)
        self.assertEqual(reader.read(), (False, HardwareStatus.OK))
        reader.invalidate()
        self.assertIsNone(reader.candidate)
        self.devices["event0"]["state"] = True
        self.clock += 0.1
        self.assertEqual(reader.read(), (True, HardwareStatus.OK))


if __name__ == "__main__":
    unittest.main()
