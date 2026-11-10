"""Software tent and stand use Windows tablet interaction without slate writes."""

import unittest

from panasonic_posture.model import StateSnapshot, Target
from panasonic_posture.windows.tablet_mode import (
    DESKTOP, PREFERENCE_ALWAYS_SWITCH, PREFERENCE_ASK, PREFERENCE_DONT_SWITCH,
    TABLET,
)
from panasonic_posture.windows.touch_mode import WindowsTouchMode


class Shell:
    def __init__(self, mode=DESKTOP):
        self.mode = mode
        self.writes = []
        self.prompts = []
        self.fail = False

    def read(self):
        return self.mode

    def set_mode(self, mode):
        if self.fail:
            raise OSError('shell write failed')
        self.writes.append(mode)
        self.mode = mode

    def prompt_mode(self, mode):
        if self.fail:
            raise OSError('shell prompt failed')
        self.prompts.append(mode)


def state(mode, *, hardware=False, pending=False):
    return StateSnapshot(target=Target(mode=mode), applied=Target(mode=mode),
                         hardware=hardware, posture_pending=pending,
                         session_connected=True)


class WindowsTouchModeTests(unittest.TestCase):
    def setUp(self):
        self.shell = Shell()
        self.hardware = [False]
        self.preference = [PREFERENCE_ALWAYS_SWITCH]
        self.now = 10.0
        self.touch = WindowsTouchMode(
            self.shell,
            lambda: (self.hardware[0], None),
            preference_query=lambda: self.preference[0],
            clock=lambda: self.now,
            retry_seconds=0.5,
        )

    def test_tent_and_stand_switch_once_then_restore_the_original_shell(self):
        self.touch.sync(state('tent'))
        self.touch.sync(state('stand'))
        self.assertEqual(self.shell.mode, TABLET)
        self.assertEqual(self.shell.writes, [TABLET])
        self.touch.sync(state('laptop'))
        self.assertEqual(self.shell.writes, [TABLET, DESKTOP])

    def test_windows_preferences_choose_no_switch_prompt_or_automatic_switch(self):
        self.preference[0] = PREFERENCE_DONT_SWITCH
        self.touch.sync(state('tent'))
        self.assertEqual(self.shell.mode, DESKTOP)
        self.assertEqual((self.shell.writes, self.shell.prompts), ([], []))

        self.preference[0] = PREFERENCE_ASK
        self.touch = WindowsTouchMode(
            self.shell, lambda: (self.hardware[0], None),
            preference_query=lambda: self.preference[0], clock=lambda: self.now,
            retry_seconds=0.5,
        )
        self.touch.sync(state('tent'))
        self.assertEqual(self.shell.mode, DESKTOP)
        self.assertEqual(self.shell.writes, [])
        self.assertEqual(self.shell.prompts, [TABLET])

        self.preference[0] = PREFERENCE_ALWAYS_SWITCH
        self.touch = WindowsTouchMode(
            self.shell, lambda: (self.hardware[0], None),
            preference_query=lambda: self.preference[0], clock=lambda: self.now,
            retry_seconds=0.5,
        )
        self.touch.sync(state('tent'))
        self.assertEqual(self.shell.mode, TABLET)
        self.assertEqual(self.shell.writes, [TABLET])

    def test_unconfirmed_or_failed_target_does_not_switch_the_shell(self):
        pending = StateSnapshot(target=Target(mode='tent'), applied=Target(mode='laptop'),
                                hardware=False, session_connected=True)
        self.touch.sync(pending)
        self.assertEqual(self.shell.writes, [])
        failed = state('tent')
        failed.error_code = 'display-unavailable'
        self.touch.sync(failed)
        self.assertEqual(self.shell.writes, [])

    def test_existing_tablet_shell_is_preserved_as_the_original_mode(self):
        self.shell.mode = TABLET
        self.touch.sync(state('tent'))
        self.assertEqual(self.shell.writes, [])
        self.touch.sync(state('laptop'))
        self.assertEqual(self.shell.mode, TABLET)
        self.assertEqual(self.shell.writes, [])

    def test_physical_handoff_and_posture_wait_do_not_restore_shell_mode(self):
        self.touch.sync(state('tent'))
        self.touch.sync(state('hardware', hardware=True))
        self.touch.sync(state('unavailable', hardware=False, pending=True))
        self.assertEqual(self.shell.writes, [TABLET])
        self.touch.sync(state('stand', hardware=False))
        self.touch.sync(state('laptop'))
        self.assertEqual(self.shell.writes, [TABLET, DESKTOP])

    def test_failed_shell_switch_retries_without_claiming_success(self):
        self.shell.fail = True
        self.touch.sync(state('tent'))
        self.assertEqual(self.shell.writes, [])
        self.touch.sync(state('tent'))
        self.assertTrue(self.shell.fail)
        self.assertEqual(self.shell.writes, [])
        self.shell.fail = False
        self.touch.sync(state('tent'))
        self.assertEqual(self.shell.writes, [])
        self.now += 0.5
        self.touch.sync(state('tent'))
        self.assertEqual(self.shell.writes, [TABLET])

    def test_close_restores_only_when_physical_tablet_is_not_active(self):
        self.touch.sync(state('tent'))
        self.hardware[0] = True
        self.touch.close()
        self.assertEqual(self.shell.mode, TABLET)
        self.hardware[0] = False
        self.touch.close()
        self.assertEqual(self.shell.mode, DESKTOP)


if __name__ == '__main__':
    unittest.main()
