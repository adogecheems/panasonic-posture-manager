"""The Windows taskbar must not inherit pythonw's shell identity."""
import unittest

from panasonic_posture.windows.app_identity import (
    APP_USER_MODEL_ID,
    configure_process_identity,
)


class AppIdentityTests(unittest.TestCase):
    def test_sets_the_stable_app_user_model_id(self):
        calls = []

        def setter(value):
            calls.append(value)
            return 0

        self.assertTrue(configure_process_identity(platform="win32", setter=setter))
        self.assertEqual(calls, [APP_USER_MODEL_ID])

    def test_identity_failure_is_non_fatal(self):
        self.assertFalse(configure_process_identity(
            platform="win32", setter=lambda _value: -2147024809
        ))

        def fail(_value):
            raise OSError("shell unavailable")

        self.assertFalse(configure_process_identity(platform="win32", setter=fail))
        self.assertFalse(configure_process_identity(platform="linux"))


if __name__ == "__main__":
    unittest.main()
