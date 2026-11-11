"""Explicit fake ports running the real engine and display transaction pipeline.

Only imported by isolated tests and the in-memory demo fixture. None of these ports opens a host device
or calls a desktop API. Applied receipts come from the shared engine after
the simulated display and input adapters have both completed.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import time

from ..controller import Controller
from ..contracts import ErrorCode
from ..model import DeviceInfo, HardwareStatus, SensorInfo, SensorReading
from ..presentation import PosePresentation
from ..service.engine import Engine
from ..service.ports import SessionOwner
from ..session.runtime import SessionRuntime
from ..session.transaction import DisplayState, SoftwareDisplay
from ..session.failures import DisplayError
from ..tasks import InlineExecutor


class MemoryJournal:
    def __init__(self):
        self.document = None

    def load(self):
        return deepcopy(self.document)

    def save(self, document):
        self.document = deepcopy(document)

    def clear(self):
        self.document = None


class SimulatedDisplay(SoftwareDisplay):
    backend_name = 'windows-simulation'

    def __init__(self):
        self.current = DisplayState(('simulated-internal-panel',), 0, False, True)
        self.fail = False
        super().__init__(journal=MemoryJournal())

    def _read_state(self):
        return self.current

    def _write_rotation(self, state, transform):
        if self.fail:
            raise DisplayError(ErrorCode.DISPLAY_TRANSFORM)
        self.current = replace(self.current, transform=transform)

    def _write_lock(self, state, locked):
        self.current = replace(self.current, lock=locked)


class SimulatedInput:
    error_code = error_detail = touch_activity = None

    def __init__(self):
        self.running = False
        self.state = None

    def keep(self, now):
        self.running = True
        self.state = SimpleNamespace(error=None, satisfies=lambda target: target.disable_internal_input)

    def follow_hardware(self, now):
        self.running = True
        self.state = SimpleNamespace(error=None, satisfies=lambda target: target.mode == 'hardware')

    def release(self):
        self.running = False
        self.state = None

    def retry(self):
        self.release()

    def request_wake(self, serial):
        raise ValueError('simulation does not emit wake events')


class Simulation:
    def __init__(self, store, instance: str, scenario: str):
        if scenario not in ('laptop', 'prompt', 'hardware', 'slow', 'error'):
            raise ValueError('unknown simulation scenario')
        self.now = time.monotonic() - 4
        self.scenario = scenario
        self.vector = (0., 0., -9.81)
        self.sensor = SimpleNamespace(info=SensorInfo(ready=True, backend='windows-simulation'),
            reading=lambda: SensorReading(self.vector, self.now))
        self.actuator = SimulatedInput()
        self.owner = SessionOwner('windows-simulation', instance)
        self.engine = Engine(controller=Controller(), sensor=self.sensor, actuator=self.actuator,
            presentation=PosePresentation(), settings=store, instance=instance,
            device=DeviceInfo(platform='windows-simulation', support_tier='compatible'),
            hardware_query=lambda: (scenario == 'hardware', HardwareStatus.OK),
            monotonic=lambda: self.now, executor=InlineExecutor())
        self.display = SimulatedDisplay()
        self.session = SessionRuntime(SimpleNamespace(
            get_state=lambda: self.engine.state,
            session_update=lambda *args: self.engine.session_update(*args, owner=self.owner)),
            self.display, None, executor=InlineExecutor(), lease_executor=InlineExecutor())
        # Establish a known synthetic sample history without waiting on a host
        # sensor. Production timing constants are used without modification.
        for index in range(80):
            if index == 40 and scenario == 'prompt':
                self.vector = (0., -9.81, 0.)
            self.advance(self.now + .05)
        self.started = self.now
        if scenario in ('slow', 'error'):
            self.display.fail = scenario == 'error'
            self.engine.set_mode('tent')
            self.engine.tick()

    def advance(self, now: float):
        self.now = now
        self.engine.tick()
        if self.scenario == 'slow' and hasattr(self, 'started') and now - self.started < 10:
            self.engine.session_update(self.engine.state.generation, False, owner=self.owner)
        else:
            self.session.tick(now)
        self.engine.tick()

    def call(self, method: str, params: dict):
        if method == 'set_mode':
            if not isinstance(params['mode'], str):
                raise ValueError('mode must be a string')
            self.engine.set_mode(params['mode'])
        elif method == 'retry':
            fallback = params['fallback_mode']
            if fallback is not None and not isinstance(fallback, str):
                raise ValueError('fallback mode must be a string or null')
            self.display.fail = False
            if fallback is not None:
                self.engine.set_mode(fallback)
            self.engine.retry()
        elif method == 'confirm_prompt':
            prompt, epoch = params['prompt'], params['epoch']
            if prompt not in ('tent', 'stand') or type(epoch) is not int or epoch < 0:
                raise ValueError('invalid prompt')
            (self.engine.confirm_tent if prompt == 'tent' else self.engine.confirm_stand)(epoch)
        elif method == 'calibrate':
            self.engine.calibrate_sensor()
        elif method == 'reset_calibration':
            self.engine.reset_sensor_calibration()
        elif method == 'set_preference':
            getattr(self.engine, 'set_' + params['field'])(params['enabled'])
        else:
            raise ValueError('unknown method')
        self.engine.tick()

    def close(self):
        self.session.close()
        self.engine.shutdown()
