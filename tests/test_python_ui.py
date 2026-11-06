"""Regression coverage for the Python UI; widgets run on an isolated display."""
import json
import unittest
from panasonic_posture.ui.service import parse_state
from panasonic_posture.ui.posture import displayed_pose


class StateTests(unittest.TestCase):
    def test_validated_state(self):
        state = parse_state(json.dumps({'version': 2, 'generation': 1, 'target': {'mode': 'laptop'}}))
        self.assertFalse(state['posture_pending'])
        for value in ([], {'version': 9}, {'version': 2, 'target': []},
                      {'version': 2, 'generation': True, 'target': {}}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_state(json.dumps(value))

    def test_displayed_pose(self):
        self.assertEqual(displayed_pose({'applied': {'mode': 'hardware'}}), 'tablet')
        state = {'manual': 'tent', 'target': {'mode': 'tent'}, 'pose': 'laptop'}
        self.assertEqual(displayed_pose(state), 'tent')
        self.assertEqual(displayed_pose(dict(state, error_code='internal-error')), 'laptop')
