"""Read-only Windows sensor diagnostics with fake process and JSON boundaries."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from panasonic_posture.windows.probe import parse_sample, probe, SensorProbeError


def sample(**changes):
    value = dict(version=1, device='sensor-1', minimum_report_interval_ms=10,
                 timestamp='2026-11-08T12:00:00.0000000+00:00', x=0., y=0., z=-1.)
    value.update(changes)
    return value


class WindowsProbeTests(unittest.TestCase):
    def test_normalizes_g_without_changing_axis_signs(self):
        result = parse_sample(sample(x=.25, y=-.5, z=-1.))
        self.assertEqual(result['acceleration_g'], [.25, -.5, -1.])
        self.assertAlmostEqual(result['acceleration_m_s2'][0], 2.4516625)
        self.assertAlmostEqual(result['acceleration_m_s2'][1], -4.903325)
        self.assertAlmostEqual(result['acceleration_m_s2'][2], -9.80665)

    def test_rejects_missing_identity_and_untrusted_values(self):
        for changes in ({'version': True}, {'device': ''}, {'timestamp': None},
                        {'x': True}, {'y': float('nan')}, {'z': float('inf')},
                        {'x': 10**500},
                        {'minimum_report_interval_ms': -1}):
            with self.subTest(changes=changes), self.assertRaises(SensorProbeError):
                parse_sample(sample(**changes))

    def test_process_output_is_bounded_and_uses_fixed_windows_program(self):
        stdout = json.dumps(sample()).encode() + b'\n'
        with patch('panasonic_posture.windows.probe.sys.platform', 'win32'), \
             patch('panasonic_posture.windows.probe.subprocess.run',
                   return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=b'')) as run:
            result = probe(samples=1)
        self.assertEqual(len(result), 1)
        args = run.call_args.args[0]
        self.assertTrue(args[0].lower().endswith('powershell.exe'))
        self.assertIn('-EncodedCommand', args)
        self.assertNotIn('shell', run.call_args.kwargs)
        self.assertIsNone(run.call_args.kwargs.get('input'))

    def test_non_windows_and_unbounded_request_do_not_start_a_process(self):
        with patch('panasonic_posture.windows.probe.subprocess.run') as run:
            for count in (0, 101, True):
                with self.assertRaises(SensorProbeError):
                    probe(samples=count)
            with patch('panasonic_posture.windows.probe.sys.platform', 'linux'):
                with self.assertRaises(SensorProbeError):
                    probe(samples=1)
        run.assert_not_called()

    def test_device_change_or_missing_samples_is_not_reported_as_success(self):
        for stdout in (b'', json.dumps(sample()).encode() + b'\n',
                       (json.dumps(sample()) + '\n' + json.dumps(sample(device='another')) + '\n').encode()):
            with patch('panasonic_posture.windows.probe.sys.platform', 'win32'), \
                 patch('panasonic_posture.windows.probe.subprocess.run',
                       return_value=SimpleNamespace(returncode=0, stdout=stdout, stderr=b'')):
                with self.assertRaises(SensorProbeError):
                    probe(samples=2)


if __name__ == '__main__':
    unittest.main()
