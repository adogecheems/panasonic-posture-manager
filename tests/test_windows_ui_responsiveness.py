"""UI responsiveness tuning: idle refresh cadence and lazy page construction."""
import time
import unittest

from panasonic_posture.model import StateSnapshot
from panasonic_posture.windows.ipc.client import (
    HardwareUIClient,
    IDLE_REFRESH_SECONDS,
)

try:
    from PyQt6.QtWidgets import QApplication
except ModuleNotFoundError:
    QApplication = None


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class Connection:
    def __init__(self):
        self.state = StateSnapshot(instance='one', hardware=False,
                                   sensor_source='windows-winrt')
        self.calls = []
        self.closed = False

    def request(self, method, params):
        self.calls.append((method, params))
        return dict(instance=self.state.instance, accepted=True,
                    state=self.state.to_dict())

    def close(self):
        self.closed = True


class IdleRefreshTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.connection = Connection()
        self.states, self.unavailable = [], []
        self.client = HardwareUIClient(
            factory=lambda: self.connection, clock=self.clock,
        )
        self.client.start(
            lambda state: self.states.append(state),
            lambda: self.unavailable.append(True),
        )
        self.addCleanup(self.stop)

    def stop(self):
        self.client.stop()
        self.client._thread.join(timeout=5)

    def until(self, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            self.client.poll()
            if predicate():
                return
            time.sleep(.01)
        self.fail('hardware UI client did not reach expected state')

    def test_idle_refresh_aligns_with_the_service_cadence(self):
        self.until(lambda: bool(self.states))
        self.assertEqual([call[0] for call in self.connection.calls],
                         ['get_state'])
        self.clock.advance(IDLE_REFRESH_SECONDS - 0.01)
        self.client.poll()
        self.assertEqual(len(self.connection.calls), 1)
        self.clock.advance(0.02)
        self.client.poll()
        self.until(lambda: len(self.connection.calls) >= 2)
        self.assertEqual(self.connection.calls[-1][0], 'get_state')
        self.assertFalse(self.unavailable)


@unittest.skipIf(QApplication is None, 'PyQt6 is not installed')
class LazySettingsTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        self.app.processEvents()

    def test_settings_page_builds_on_first_use_and_receives_latest_state(self):
        from panasonic_posture.windows.ui.app import Window
        from panasonic_posture.windows.ui.unavailable import UnavailableClient

        client = UnavailableClient()
        window = Window(client=client, language='en', start_client=False)
        self.addCleanup(window.shutdown)
        try:
            self.assertIsNone(window._settings)
            self.assertEqual(window.stackedWidget.count(), 1)
            state = StateSnapshot(instance='one', hardware=False,
                                  sensor_source='windows-winrt').to_dict()
            window.update_state(state)
            self.assertIsNone(window._settings)
            settings = window.settings
            self.assertIs(window._settings, settings)
            self.assertEqual(window.stackedWidget.count(), 2)
            self.assertEqual(settings.model.state, state)
        finally:
            window.shutdown()


@unittest.skipIf(QApplication is None, 'PyQt6 is not installed')
class WakeWatchTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])

    def tearDown(self):
        self.app.processEvents()

    def test_watch_refreshes_on_publication_and_closes(self):
        from panasonic_posture.windows.ui.wake import WakeWatch

        class FakeSignal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

        class FakeNotifier:
            def __init__(self, handle, parent):
                del parent
                self.handle = handle
                self.activated = FakeSignal()
                self.enabled = True
                self.deleted = False

            def setEnabled(self, enabled):
                self.enabled = enabled

            def deleteLater(self):
                self.deleted = True

        class FakeWake:
            def __init__(self):
                self.opened = False
                self.closed = False
                self.handle = 7

            def open(self):
                self.opened = True

            def close(self):
                self.closed = True

        class FakeClient:
            def __init__(self):
                self.refreshes = 0

            def refresh(self):
                self.refreshes += 1

        client = FakeClient()
        wake = FakeWake()
        watch = WakeWatch(client, wake=wake, notifier_factory=FakeNotifier)
        self.assertTrue(wake.opened)
        notifier = watch._notifier
        notifier.activated.slots[0]()
        self.assertEqual(client.refreshes, 1)
        watch.close()
        self.assertTrue(wake.closed)
        self.assertTrue(notifier.deleted)

    def test_unavailable_event_leaves_watch_inert(self):
        from panasonic_posture.windows.ui.wake import WakeWatch

        class MissingWake:
            def open(self):
                raise OSError('event not found')

            def close(self):
                self.closed = True

        class FakeClient:
            def refresh(self):
                raise AssertionError('inert watch must not refresh')

        watch = WakeWatch(FakeClient(), wake=MissingWake())
        self.assertIsNone(watch._notifier)
        watch.close()

    def test_real_notifier_receives_publication(self):
        from PyQt6.QtCore import QEventLoop, QTimer
        from panasonic_posture.windows.ipc.wake import WakeEvent
        from panasonic_posture.windows.ui.wake import WakeWatch

        class FakeClient:
            def __init__(self):
                self.refreshes = 0

            def refresh(self):
                self.refreshes += 1

        name = 'PanasonicPostureManager.Wake.UiTest'
        publisher = WakeEvent(name=name)
        publisher.create()
        self.addCleanup(publisher.close)
        client = FakeClient()
        watch = WakeWatch(client, wake=WakeEvent(name=name))
        self.addCleanup(watch.close)
        publisher.publish()
        loop = QEventLoop()
        QTimer.singleShot(3000, loop.quit)
        loop.exec()
        self.assertEqual(client.refreshes, 1)


if __name__ == '__main__':
    unittest.main()
