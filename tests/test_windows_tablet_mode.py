"""Read-only Windows shell observation without touching the live shell."""
import ctypes as C
from contextlib import contextmanager
from pathlib import Path
import unittest
from unittest import mock

from panasonic_posture.windows import tablet_mode

_FUNCTYPE = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)


def _calls(current, *, query_result=0):
    reads = []

    @_FUNCTYPE(C.c_long, C.c_void_p, C.POINTER(C.c_int))
    def query(this, destination):
        del this
        reads.append(1)
        destination[0] = current['mode']
        return query_result

    vtable = (C.c_void_p * 4)(
        None, None, None, C.cast(query, C.c_void_p).value,
    )
    return vtable, reads


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

    def test_read_uses_only_the_observation_vtable_slot(self):
        state = {'mode': tablet_mode.TABLET}
        vtable, reads = _calls(state)

        @contextmanager
        def controller():
            yield vtable, None

        with mock.patch.object(tablet_mode, '_controller', controller):
            bridge = tablet_mode.WindowsShellMode()
            self.assertEqual(bridge.read(), tablet_mode.TABLET)
        self.assertEqual(reads, [1])

    def test_query_failure_and_invalid_modes_are_rejected(self):
        for mode, result in ((-1, 0), (2, 0), (tablet_mode.TABLET, -2147467259)):
            state = {'mode': mode}
            vtable, reads = _calls(state, query_result=result)

            @contextmanager
            def controller():
                yield vtable, None

            with self.subTest(mode=mode, result=result), \
                    mock.patch.object(tablet_mode, '_controller', controller), \
                    self.assertRaises(tablet_mode.TabletModeError):
                tablet_mode.WindowsShellMode().read()
            self.assertEqual(reads, [1])

    def test_adapter_has_no_shell_switching_api(self):
        self.assertFalse(hasattr(tablet_mode.WindowsShellMode, 'write'))
        source = Path(tablet_mode.__file__).read_text(encoding='utf-8')
        self.assertNotIn('SetMode', source)
        self.assertNotIn('_TRIGGER_API', source)


if __name__ == '__main__':
    unittest.main()
