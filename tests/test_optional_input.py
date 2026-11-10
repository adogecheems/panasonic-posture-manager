"""Screen-only execution must never fake input ownership or bypass receipts."""
from types import SimpleNamespace
import unittest

from panasonic_posture.controller import Controller
from panasonic_posture.model import DeviceInfo, HardwareStatus, SensorInfo, SensorReading
from panasonic_posture.presentation import PosePresentation
from panasonic_posture.service.engine import Engine
from panasonic_posture.service.ports import SessionOwner, ActuatorError
from panasonic_posture.session.runtime import SessionRuntime
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.display_only_input import DisplayOnlyInput
from panasonic_posture.windows.simulation import SimulatedDisplay


class OptionalInputTests(unittest.TestCase):
    def setUp(self):
        self.now, self.hardware = 10., False
        self.input = DisplayOnlyInput()
        self.controller = Controller(input_control=False)
        self.engine = Engine(controller=self.controller, presentation=PosePresentation(),
            sensor=SimpleNamespace(info=SensorInfo(ready=True, backend='windows-fixture'),
                reading=lambda: SensorReading((0., 0., -9.81), self.now)),
            actuator=self.input, settings=None, device=DeviceInfo(platform='windows-fixture'),
            hardware_query=lambda: (self.hardware, HardwareStatus.OK),
            monotonic=lambda: self.now, executor=InlineExecutor())
        self.addCleanup(self.engine.shutdown)
        for _ in range(80):
            self.now += .05
            self.engine.tick()
        self.display = SimulatedDisplay()
        self.owner = SessionOwner('windows-fixture', 'screen-session')
        self.runtime = SessionRuntime(SimpleNamespace(get_state=lambda: self.engine.state,
            session_update=lambda *args: self.engine.session_update(*args, owner=self.owner)),
            self.display, None, executor=InlineExecutor(), lease_executor=InlineExecutor())
        self.addCleanup(self.runtime.close)

    def step(self):
        self.now += .1
        self.engine.tick()
        self.runtime.tick(self.now)
        self.engine.tick()

    def test_screen_modes_require_real_display_receipts_and_never_disable_input(self):
        for mode, rotation in (('laptop', 0), ('tent', 2), ('stand', 0)):
            with self.subTest(mode=mode):
                self.engine.set_mode(mode)
                self.engine.tick()
                self.assertIsNone(self.engine.state.applied)
                self.step()
                self.assertEqual(self.engine.state.applied.mode, mode)
                self.assertFalse(self.engine.state.applied.disable_internal_input)
                self.assertFalse(self.engine.state.target.disable_internal_input)
                self.assertEqual(self.display.current.transform, rotation)
                self.assertFalse(self.input.running)

    def test_display_failure_does_not_become_applied_without_input_control(self):
        self.display.fail = True
        self.engine.set_mode('tent')
        with self.assertLogs('panasonic_posture.session.runtime', level='WARNING'):
            self.step()
        self.assertIsNone(self.engine.state.applied)
        self.assertFalse(self.input.running)
        self.assertEqual(self.display.current.transform, 0)

    def test_stale_generation_and_expired_lease_cannot_confirm_screen_only_modes(self):
        self.engine.set_mode('tent')
        self.engine.tick()
        generation = self.engine.state.generation
        self.assertFalse(self.engine.session_update(generation + 1, True, None, None, self.owner))
        self.engine.tick()
        self.assertIsNone(self.engine.state.applied)
        self.step()
        self.assertEqual(self.engine.state.applied.mode, 'tent')
        self.now += 4
        self.engine.tick()
        self.assertIsNone(self.engine.state.applied)
        self.runtime.close()
        self.assertEqual(self.display.current.transform, 0)

    def test_physical_tablet_priority_still_overrides_manual_screen_mode(self):
        self.engine.set_mode('tent')
        self.step()
        self.hardware = True
        for _ in range(10): self.step()
        self.assertIs(self.engine.state.hardware, True)
        self.assertEqual(self.engine.state.manual, 'auto')
        self.assertEqual(self.engine.state.applied.mode, 'hardware')
        self.assertFalse(self.engine.state.applied.disable_internal_input)
        self.assertFalse(self.input.running)

    def test_input_operations_are_explicitly_unavailable(self):
        for method, args in ((self.input.keep, (self.now,)), (self.input.follow_hardware, (self.now,)),
                             (self.input.request_wake, (1,))):
            with self.assertRaises(ActuatorError): method(*args)
        self.assertFalse(self.input.running)

    def test_existing_platform_default_still_requires_input_control(self):
        controller = Controller()
        controller.set_manual('tent')
        for index in range(80):
            controller.update(index * .05, SensorReading((0., 0., -9.81), index * .05), False)
        self.assertTrue(controller.target.disable_internal_input)
        for value in (None, 0, 'false'):
            with self.assertRaises(ValueError): Controller(input_control=value)
