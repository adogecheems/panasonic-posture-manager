"""The native-session qualification fence fails closed in every direction."""
import unittest

from panasonic_posture.model import StateSnapshot
from panasonic_posture.session.failures import FailureKind, ServiceError
from panasonic_posture.windows.session.qualification import QualifiedSessionClient


class Context:
    def __init__(self):
        self.available = True
    def control_available(self):
        return self.available


class Client:
    def __init__(self):
        self.state = StateSnapshot(instance='one', generation=4, session_connected=True)
        self.updates = []
        self.calls = []
    def get_state(self):
        return self.state
    def session_update(self, generation, ready, error_code=None, error_detail=None):
        self.calls.append(('update', generation, ready, error_code, error_detail))
    def confirm_tent(self, epoch):
        self.calls.append(('tent', epoch))
    def confirm_stand(self, epoch):
        self.calls.append(('stand', epoch))
    def wake_screen(self, generation, serial):
        self.calls.append(('wake', generation, serial))


class QualifiedSessionClientTests(unittest.TestCase):
    def setUp(self):
        self.context, self.client = Context(), Client()
        self.qualified = QualifiedSessionClient(self.client, self.context)

    def test_lost_qualification_revokes_the_same_generation_before_rejecting(self):
        self.context.available = False
        with self.assertRaises(ServiceError) as caught:
            self.qualified.get_state()
        self.assertEqual(caught.exception.kind, FailureKind.REJECTED)
        self.assertEqual(self.client.calls, [
            ('update', 4, False, 'display-unavailable', 'native-session-unavailable'),
        ])

    def test_qualification_is_rechecked_after_transmitting_a_receipt(self):
        send = self.client.session_update
        def send_then_lose(generation, ready, error_code=None, error_detail=None):
            send(generation, ready, error_code, error_detail)
            self.context.available = False
        self.client.session_update = send_then_lose
        with self.assertRaises(ServiceError):
            self.qualified.session_update(4, True)
        self.assertEqual(self.client.calls, [('update', 4, True, None, None)])

    def test_actions_never_reach_the_transport_after_qualification_loss(self):
        self.context.available = False
        for call in (
            lambda: self.qualified.confirm_tent(3),
            lambda: self.qualified.confirm_stand(3),
            lambda: self.qualified.wake_screen(4, 9),
        ):
            with self.subTest(call=call), self.assertRaises(ServiceError):
                call()
        self.assertEqual(self.client.calls, [])


if __name__ == '__main__':
    unittest.main()
