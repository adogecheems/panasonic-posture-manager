"""Windows session boundaries discard posture state before accepting more work."""
from types import SimpleNamespace
import unittest

from panasonic_posture.windows.service.host import HardwareHost


class Engine:
    def __init__(self):
        self.state = SimpleNamespace(instance='session-reset-fixture')
        self.hardware_release_pose = None
        self.resets = 0
        self.revocations = 0
        self.ticks = 0

    def reset_after_session_context_loss(self):
        self.resets += 1

    def revoke_session(self, owner):
        del owner
        self.revocations += 1

    def tick(self):
        self.ticks += 1


class SessionResetTests(unittest.TestCase):
    def host(self, *, active, admission=True):
        engine = Engine()
        admitted = [admission]
        host = HardwareHost(
            engine,
            authorize=lambda peer: active[0],
            session_process=SimpleNamespace(owner='fixture-owner'),
            session_admission=lambda: admitted[0],
        )
        return engine, host, admitted

    def test_qualification_loss_resets_once_even_if_the_pipe_already_cleared_the_owner(self):
        active = [True]
        engine, host, _ = self.host(active=active)

        host.disconnected(host.session_process)
        active[0] = False
        host.tick()
        host.tick()

        self.assertEqual(engine.resets, 1)
        self.assertGreaterEqual(engine.revocations, 1)

    def test_physical_source_loss_resets_once_until_admission_returns(self):
        active = [True]
        engine, host, admitted = self.host(active=active)
        host.tick()

        admitted[0] = False
        host.tick()
        host.tick()
        self.assertEqual(engine.resets, 1)

        admitted[0] = True
        host.tick()
        self.assertEqual(engine.resets, 1)


if __name__ == '__main__':
    unittest.main()
