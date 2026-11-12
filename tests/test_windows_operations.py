"""Shared engine-operation mapping used by every Windows entry point."""
import unittest

from panasonic_posture.windows.operations import dispatch_engine_operation


class Engine:
    def __init__(self):
        self.calls = []

    def set_mode(self, mode):
        self.calls.append(('mode', mode))

    def set_tent_enabled(self, enabled):
        self.calls.append(('tent_enabled', enabled))

    def retry(self):
        self.calls.append(('retry',))

    def confirm_tent(self, epoch):
        self.calls.append(('confirm_tent', epoch))

    def calibrate_sensor(self):
        self.calls.append(('calibrate',))

    def reset_sensor_calibration(self):
        self.calls.append(('reset_calibration',))


class DispatchEngineOperationTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()

    def dispatch(self, method, **params):
        dispatch_engine_operation(self.engine, method, params)

    def test_routes_each_validated_operation_to_the_shared_engine(self):
        self.dispatch('set_mode', mode='tent')
        self.dispatch('set_preference', field='tent_enabled', enabled=False)
        self.dispatch('retry', fallback_mode=None)
        self.dispatch('confirm_prompt', prompt='tent', epoch=7)
        self.dispatch('calibrate')
        self.dispatch('reset_calibration')
        self.assertEqual(self.engine.calls, [
            ('mode', 'tent'),
            ('tent_enabled', False),
            ('retry',),
            ('confirm_tent', 7),
            ('calibrate',),
            ('reset_calibration',),
        ])

    def test_retry_hook_and_fallback_keep_the_legacy_order(self):
        events = []
        engine = self.engine
        engine.set_mode = lambda mode: events.append(('mode', mode))
        engine.retry = lambda: events.append(('retry',))
        dispatch_engine_operation(
            engine,
            'retry',
            {'fallback_mode': 'stand'},
            before_retry=lambda: events.append(('hook',)),
        )
        self.assertEqual(events, [('hook',), ('mode', 'stand'), ('retry',)])

    def test_transport_only_operations_are_not_silently_ignored(self):
        with self.assertRaisesRegex(ValueError, 'unknown method'):
            self.dispatch('session_update', generation=1)

    def test_unknown_preference_and_prompt_fail_explicitly(self):
        with self.assertRaisesRegex(ValueError, 'unknown preference'):
            self.dispatch('set_preference', field='sideways', enabled=True)
        with self.assertRaisesRegex(ValueError, 'unknown prompt'):
            self.dispatch('confirm_prompt', prompt='folded', epoch=1)


if __name__ == '__main__':
    unittest.main()
