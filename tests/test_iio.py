"""Tests for panasonic_posture.hardware.iio (type parsing, channel order, IO)."""

import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from panasonic_posture.hardware import iio


def write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(f"{value}\n", encoding="utf-8")


def make_device(
    root,
    index=0,
    name="accel_3d",
    *,
    types=None,
    indices=None,
    scale="0.5",
    offset="0",
    mount=None,
    buffer_enabled="0",
):
    device = Path(root) / f"iio:device{index}"
    scan = device / "scan_elements"
    scan.mkdir(parents=True, exist_ok=True)
    types = types or {
        "x": "le:s16/16>>0",
        "y": "le:s16/16>>0",
        "z": "le:s16/16>>0",
    }
    for axis in ("x", "y", "z"):
        write(scan / f"in_accel_{axis}_type", types[axis])
        write(scan / f"in_accel_{axis}_en", "0")
        if indices and axis in indices:
            write(scan / f"in_accel_{axis}_index", indices[axis])
    write(scan / "in_timestamp_en", "0")
    write(device / "name", name)
    write(device / "in_accel_scale", scale)
    if offset is not None:
        write(device / "in_accel_offset", offset)
    for attribute, value in (
        ("buffer/length", 1),
        ("buffer/watermark", 1),
        ("buffer/enable", buffer_enabled),
        ("trigger/current_trigger", ""),
    ):
        write(device / attribute, value)
    if mount is not None:
        write(device / "in_accel_mount_matrix", mount)
    trigger = Path(root) / "trigger0"
    trigger.mkdir(parents=True, exist_ok=True)
    write(trigger / "name", f"{name}-dev{index}")
    return device


class ParseTypeTests(unittest.TestCase):
    def test_little_endian_signed_32(self):
        kind = iio.parse_type("le:s32/32>>0")
        self.assertEqual(kind.byteorder, "little")
        self.assertTrue(kind.signed)
        self.assertEqual(kind.bits, 32)
        self.assertEqual(kind.storage, 32)
        self.assertEqual(kind.shift, 0)
        self.assertEqual(kind.size, 4)

    def test_big_endian_unsigned_32(self):
        kind = iio.parse_type("be:u32/32>>0")
        self.assertEqual(kind.byteorder, "big")
        self.assertFalse(kind.signed)

    def test_16_and_8_and_64_bit_storage(self):
        self.assertEqual(iio.parse_type("le:s16/16>>0").size, 2)
        self.assertEqual(iio.parse_type("le:u8/8>>0").size, 1)
        self.assertEqual(iio.parse_type("le:s64/64>>0").size, 8)

    def test_mask_and_shift_type(self):
        # 24 meaningful bits in a 32-bit word, shifted right by 8.
        kind = iio.parse_type("le:s24/32>>8")
        self.assertEqual(kind.bits, 24)
        self.assertEqual(kind.storage, 32)
        self.assertEqual(kind.shift, 8)

    def test_missing_endian_prefix_defaults_to_little(self):
        self.assertEqual(iio.parse_type("s16/16>>0").byteorder, "little")

    def test_invalid_types_are_rejected(self):
        for text in (
            "",
            "le:s32",  # missing storage
            "le:x32/32>>0",  # bad sign
            "le:s32/24>>0",  # unsupported storage
            "le:s65/64>>0",  # more meaningful bits than storage
            "le:s32/32>>1",  # shift does not fit
            "le:s0/16>>0",  # zero bits
            "le:s16/16>>-1",
            "le:s16/16",  # missing shift
            "xle:s16/16>>0",
        ):
            with self.assertRaises(ValueError, msg=repr(text)):
                iio.parse_type(text)


class DecodeTests(unittest.TestCase):
    def test_little_endian_signed_32(self):
        kind = iio.parse_type("le:s32/32>>0")
        data = struct.pack("<iii", -1, 0, 123456)
        self.assertEqual(iio.decode_sample(data, (kind, kind, kind)),
                         (-1, 0, 123456))

    def test_little_endian_signed_16(self):
        kind = iio.parse_type("le:s16/16>>0")
        data = struct.pack("<hhh", -1, 0, 1234)
        self.assertEqual(iio.decode_sample(data, (kind, kind, kind)),
                         (-1, 0, 1234))

    def test_big_endian_unsigned(self):
        kind = iio.parse_type("be:u32/32>>0")
        data = struct.pack(">III", 1, 2, 3)
        self.assertEqual(iio.decode_sample(data, (kind, kind, kind)), (1, 2, 3))

    def test_64_bit_signed(self):
        kind = iio.parse_type("le:s64/64>>0")
        data = struct.pack("<q", -2**40)
        self.assertEqual(iio.decode_sample(data, (kind,)), (-2**40,))

    def test_8_bit_unsigned(self):
        kind = iio.parse_type("le:u8/8>>0")
        self.assertEqual(iio.decode_sample(b"\xff", (kind,)), (255,))

    def test_masked_bits_ignore_padding(self):
        kind = iio.parse_type("le:u12/16>>0")
        self.assertEqual(kind.decode(struct.pack("<H", 0xF123)), 0x123)

    def test_shift_moves_value_into_place(self):
        kind = iio.parse_type("le:s24/32>>8")
        raw = struct.pack("<I", 0xFFABCDEF)
        # (0xFFABCDEF >> 8) & 0xFFFFFF = 0xFFABCD, sign bit (23) set -> negative.
        self.assertEqual(kind.decode(raw), 0xFFABCD - 0x1000000)

    def test_sign_extension_of_narrow_field(self):
        kind = iio.parse_type("le:s12/16>>0")
        self.assertEqual(kind.decode(struct.pack("<H", 0x0FFF)), -1)
        self.assertEqual(kind.decode(struct.pack("<H", 0x07FF)), 0x7FF)

    def test_rejects_short_or_long_samples(self):
        kind = iio.parse_type("le:s32/32>>0")
        with self.assertRaises(ValueError):
            iio.decode_sample(b"\0" * 8, (kind, kind, kind))
        with self.assertRaises(ValueError):
            iio.decode_sample(b"\0" * 16, (kind, kind, kind))


class ChannelLayoutTests(unittest.TestCase):
    def test_channel_indices_define_buffer_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_device(
                tmp,
                indices={"x": 2, "y": 0, "z": 1},
                types={"x": "le:s16/16>>0", "y": "le:u8/8>>0",
                       "z": "le:s32/32>>0"},
            )
            axes, types = iio.scan_plan(device)
        self.assertEqual(axes, ("y", "z", "x"))
        self.assertEqual([kind.size for kind in types], [1, 4, 2])

    def test_missing_indices_fall_back_to_declared_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_device(tmp)
            axes, types = iio.scan_plan(device)
        self.assertEqual(axes, ("x", "y", "z"))
        self.assertEqual(len(types), 3)

    def test_duplicate_indices_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_device(tmp, indices={"x": 0, "y": 0, "z": 1})
            with self.assertRaises(iio.Unavailable):
                iio.scan_plan(device)

    def test_missing_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_device(tmp)
            (device / "scan_elements" / "in_accel_z_type").unlink()
            with self.assertRaises(iio.Unavailable):
                iio.scan_plan(device)


class MountMatrixTests(unittest.TestCase):
    def test_semicolon_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "in_accel_mount_matrix", "1, 0, 0; 0, 1, 0; 0, 0, 1")
            matrix = iio.read_iio_mount(tmp)
        self.assertEqual(matrix, (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0))

    def test_comma_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            write(Path(tmp) / "in_accel_mount_matrix", "0,-1,0,1,0,0,0,0,1")
            matrix = iio.read_iio_mount(tmp)
        self.assertEqual(matrix, (0.0, -1.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0))

    def test_missing_or_invalid_matrix_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(iio.read_iio_mount(tmp))
            write(Path(tmp) / "in_accel_mount_matrix", "1 2 3")
            self.assertIsNone(iio.read_iio_mount(tmp))
            write(Path(tmp) / "in_accel_mount_matrix", "1 2 3 4 5 6 7 8 nan")
            self.assertIsNone(iio.read_iio_mount(tmp))
            write(Path(tmp) / "in_accel_mount_matrix", "1 2 3 4 5 6 7 8 x")
            self.assertIsNone(iio.read_iio_mount(tmp))


class AccelSessionTests(unittest.TestCase):
    def make_accel(self, tmp, *, stop=None, mount=None, **kwargs):
        device = make_device(tmp, **kwargs)
        return device, iio.Accel(
            stop or threading.Event(), "accel_3d", mount, root=tmp
        )

    def test_setup_requires_a_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_device(tmp)
            trigger = Path(tmp) / "trigger0" / "name"
            trigger.unlink()
            accel = iio.Accel(threading.Event(), "accel_3d", root=tmp)
            with self.assertRaises(iio.Unavailable):
                accel.setup()

    def test_setup_rejects_zero_or_non_finite_scale(self):
        for scale in ("0", "-0.0", "nan", "inf"):
            with self.subTest(scale=scale), tempfile.TemporaryDirectory() as tmp:
                make_device(tmp, scale=scale)
                accel = iio.Accel(threading.Event(), "accel_3d", root=tmp)
                with self.assertRaises(iio.Unavailable):
                    accel.setup()

    def test_setup_rejects_a_busy_buffer(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_device(tmp, buffer_enabled="1")
            accel = iio.Accel(threading.Event(), "accel_3d", root=tmp)
            with self.assertRaises(iio.Unavailable):
                accel.setup()

    def test_missing_device_raises_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            accel = iio.Accel(threading.Event(), "accel_3d", root=tmp)
            with self.assertRaises(iio.Unavailable):
                accel.setup()

    def test_ambiguous_devices_raise_unavailable(self):
        # The sysfs fallback already requires exactly one matching device; the
        # IIO path must not silently pick the first of two identical sensors.
        with tempfile.TemporaryDirectory() as tmp:
            make_device(tmp)
            duplicate = Path(tmp) / "iio:device1"
            duplicate.mkdir()
            (duplicate / "name").write_text("accel_3d\n", encoding="utf-8")
            accel = iio.Accel(threading.Event(), "accel_3d", root=tmp)
            with self.assertRaisesRegex(iio.Unavailable, "multiple accel_3d"):
                accel.setup()

    def test_setup_failure_restores_sysfs_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_device(tmp)

            def failing_open(_path):
                raise FileNotFoundError("/dev/iio:device0")

            accel = iio.Accel(
                threading.Event(), "accel_3d", root=tmp, open_device=failing_open
            )
            with self.assertRaises(iio.Unavailable):
                accel.setup()
            self.assertEqual((device / "buffer" / "enable").read_text().strip(), "0")
            self.assertEqual(
                (device / "scan_elements" / "in_accel_x_en").read_text().strip(), "0"
            )
            self.assertEqual(
                (device / "trigger" / "current_trigger").read_text().strip(), ""
            )

    def test_setup_and_iteration_use_channel_indices(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = make_device(
                tmp,
                types={
                    "x": "le:s16/16>>0",
                    "y": "le:s16/16>>0",
                    "z": "le:s16/16>>0",
                },
                indices={"x": 2, "y": 0, "z": 1},
                scale="0.5",
            )
            read_fd, write_fd = os.pipe()
            accel = iio.Accel(
                threading.Event(), "accel_3d", root=tmp,
                open_device=lambda _path: read_fd,
            )
            try:
                accel.setup()
                # Buffer layout is y, z, x according to the indices.
                os.write(write_fd, struct.pack("<hhh", 100, 200, 300))
                iterator = iter(accel)
                vector = next(iterator)
                iterator.close()
            finally:
                os.close(write_fd)
                try:
                    os.close(read_fd)
                except OSError:
                    pass
            self.assertAlmostEqual(vector[0], 150.0)
            self.assertAlmostEqual(vector[1], 50.0)
            self.assertAlmostEqual(vector[2], 100.0)
            self.assertEqual(
                (device / "buffer" / "enable").read_text().strip(), "0"
            )

    def test_iteration_applies_mount_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_device(tmp, scale="1", offset="0")
            read_fd, write_fd = os.pipe()
            accel = iio.Accel(
                threading.Event(), "accel_3d", (0, -1, 0, 1, 0, 0, 0, 0, 1),
                root=tmp, open_device=lambda _path: read_fd,
            )
            try:
                accel.setup()
                os.write(write_fd, struct.pack("<hhh", 10, 20, 30))
                iterator = iter(accel)
                vector = next(iterator)
                iterator.close()
            finally:
                os.close(write_fd)
                try:
                    os.close(read_fd)
                except OSError:
                    pass
            # (x, y, z) -> (-y, x, z)
            self.assertEqual(vector, (-20.0, 10.0, 30.0))

    def test_no_samples_raises_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_device(tmp)
            read_fd, write_fd = os.pipe()
            accel = iio.Accel(
                threading.Event(), "accel_3d", root=tmp,
                open_device=lambda _path: read_fd,
            )
            try:
                accel.setup()
                with mock.patch.object(iio, "SAMPLE_WAIT_SECONDS", 0.01), \
                        mock.patch.object(iio, "IIO_NO_SAMPLE_SECONDS", 0.0):
                    iterator = iter(accel)
                    with self.assertRaises(iio.Unavailable):
                        next(iterator)
            finally:
                os.close(write_fd)
                try:
                    os.close(read_fd)
                except OSError:
                    pass

    def test_stop_event_ends_iteration(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_device(tmp)
            read_fd, write_fd = os.pipe()
            stop = threading.Event()
            accel = iio.Accel(
                stop, "accel_3d", root=tmp, open_device=lambda _path: read_fd
            )
            try:
                accel.setup()
                stop.set()
                with mock.patch.object(iio, "SAMPLE_WAIT_SECONDS", 0.01):
                    self.assertEqual(list(accel), [])
            finally:
                os.close(write_fd)
                try:
                    os.close(read_fd)
                except OSError:
                    pass

    def test_invalid_constructor_mount_is_rejected(self):
        with self.assertRaises(ValueError):
            iio.Accel(threading.Event(), mount=(1, 0, 0))
        with self.assertRaises(ValueError):
            iio.Accel(threading.Event(), mount=(1, 0, 0, 0, 1, 0, 0, 0, float("nan")))


class DeviceListingTests(unittest.TestCase):
    def test_iio_devices_reports_read_only_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_device(tmp, mount="1,0,0,0,1,0,0,0,1")
            make_device(tmp, index=2, name="als")
            devices = {entry["name"]: entry for entry in iio.iio_devices(tmp)}
        self.assertIn("accel_3d", devices)
        accel = devices["accel_3d"]
        self.assertEqual(accel["scan_types"]["x"], "le:s16/16>>0")
        self.assertEqual(accel["scan_indices"], {})
        self.assertEqual(accel["scale"], "0.5")
        self.assertEqual(accel["mount_matrix"], [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0])
        self.assertEqual(accel["buffer_enabled"], "0")


if __name__ == "__main__":
    unittest.main()
