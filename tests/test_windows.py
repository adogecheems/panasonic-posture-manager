"""Windows JSON transport and per-settings process lifetime checks."""
from pathlib import Path
import tempfile
import unittest

from panasonic_posture.windows.instance import InstanceLock
from panasonic_posture.windows.protocol import (
    MAX_FRAME,
    ProtocolError,
    decode,
    encode,
)


class FrameTests(unittest.TestCase):
    def test_bounded_strict_frames(self):
        value = {
            'version': 1,
            'type': 'request',
            'id': 1,
            'method': 'get_state',
            'params': {},
        }
        self.assertEqual(decode(encode(value)), value)
        for frame in (
            b'{}',
            b'[]\n',
            b'{"version":true}\n',
            b'x' * (MAX_FRAME + 1),
            b'{"version":1,"version":1}\n',
            b'{"version":1,"x":NaN}\n',
        ):
            with self.subTest(frame=frame[:40]), self.assertRaises(ProtocolError):
                decode(frame)

    def test_non_utf8_and_missing_delimiter_are_rejected(self):
        for frame in (b'{"version":1}\xff\n', b'{"version":1}'):
            with self.subTest(frame=frame), self.assertRaises(ProtocolError):
                decode(frame)


class InstanceLockTests(unittest.TestCase):
    def test_settings_lock_rejects_competing_host_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            with InstanceLock(path):
                with self.assertRaises(OSError):
                    with InstanceLock(path):
                        self.fail('competing instance acquired the same settings')
            with InstanceLock(path):
                pass


if __name__ == '__main__':
    unittest.main()
