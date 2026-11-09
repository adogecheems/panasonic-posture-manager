"""Windows shell tablet-mode COM boundary without touching the live shell."""

import ctypes as C
from contextlib import contextmanager
import unittest
from unittest import mock

from panasonic_posture.windows import tablet_mode


def _calls(current, *, query_result=0, write_result=0, apply_write=True):
    reads = []
    writes = []

    @C.WINFUNCTYPE(C.c_long, C.c_void_p, C.POINTER(C.c_int))
    def query(this, destination):
        del this
        reads.append(1)
        destination[0] = current['mode']
        return query_result

    @C.WINFUNCTYPE(C.c_long, C.c_void_p, C.c_int, C.c_int)
    def write(this, mode, trigger):
        del this
        writes.append((mode, trigger))
        if apply_write and write_result >= 0:
            current['mode'] = mode
        return write_result

    vtable = (C.c_void_p * 5)(
        None, None, None, C.cast(query, C.c_void_p).value,
        C.cast(write, C.c_void_p).value,
    )
    return vtable, reads, writes


class TabletModeTests(unittest.TestCase):
    def test_guid_parser_preserves_windows_byte_layout(self):
        guid = tablet_mode._Guid.parse(
            '4FDA780A-ACD2-41F7-B4F2-EBE674C9BF2A'
        )
        self.assertEqual(C.sizeof(guid), 16)
        self.assertEqual(guid.data1, 0x4FDA780A)
        self.assertEqual(guid.data2, 0xACD2)
        self.assertEqual(guid.data3, 0x41F7)
        self.assertEqual(bytes(guid.data4), bytes.fromhex('B4F2EBE674C9BF2A'))

    def test_hresult_success_accepts_success_codes_only(self):
        self.assertTrue(tablet_mode._succeeded(0))
        self.assertTrue(tablet_mode._succeeded(1))
        self.assertFalse(tablet_mode._succeeded(-2147467259))

    def test_read_and_verified_write_use_the_documented_vtable_slots(self):
        state = {'mode': tablet_mode.DESKTOP}
        vtable, reads, writes = _calls(state)

        @contextmanager
        def controller():
            yield vtable, None

        with mock.patch.object(tablet_mode, '_controller', controller):
            bridge = tablet_mode.WindowsTabletMode()
            self.assertEqual(bridge.read(), tablet_mode.DESKTOP)
            bridge.write(tablet_mode.TABLET)
            self.assertEqual(bridge.read(), tablet_mode.TABLET)

        self.assertEqual(reads, [1, 1, 1])
        self.assertEqual(writes, [(tablet_mode.TABLET, tablet_mode._TRIGGER_API)])

    def test_invalid_mode_is_rejected_before_opening_com_and_booleans_are_not_modes(self):
        with mock.patch.object(
                tablet_mode, '_controller',
                side_effect=AssertionError('COM must not be opened')):
            bridge = tablet_mode.WindowsTabletMode()
            for invalid in (True, False, -1, 2, 'tablet'):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(ValueError):
                        bridge.write(invalid)

    def test_write_failure_is_reported_without_polling_read_back(self):
        state = {'mode': tablet_mode.DESKTOP}
        vtable, reads, writes = _calls(state, write_result=-2147467259)

        @contextmanager
        def controller():
            yield vtable, None

        with mock.patch.object(tablet_mode, '_controller', controller):
            with self.assertRaises(tablet_mode.TabletModeError):
                tablet_mode.WindowsTabletMode().write(tablet_mode.TABLET)
        self.assertEqual(reads, [])
        self.assertEqual(len(writes), 1)

    def test_read_back_mismatch_has_a_fixed_bounded_retry_count(self):
        state = {'mode': tablet_mode.DESKTOP}
        vtable, reads, writes = _calls(state, apply_write=False)

        @contextmanager
        def controller():
            yield vtable, None

        with mock.patch.object(tablet_mode, '_controller', controller), \
                mock.patch.object(tablet_mode.time, 'sleep') as sleep:
            with self.assertRaises(tablet_mode.TabletModeError):
                tablet_mode.WindowsTabletMode().write(tablet_mode.TABLET)
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(reads), 10)
        self.assertEqual(sleep.call_count, 10)


if __name__ == '__main__':
    unittest.main()
