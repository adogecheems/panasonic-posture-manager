"""Synthetic Windows ports used only by automated tests."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import time
import uuid

from panasonic_posture.controller import Controller
from panasonic_posture.contracts import ErrorCode
from panasonic_posture.model import (
    DeviceInfo,
    HardwareStatus,
    SensorInfo,
    SensorReading,
    StateSnapshot,
)
from panasonic_posture.presentation import PosePresentation
from panasonic_posture.service.engine import Engine, ServiceError
from panasonic_posture.service.ports import SessionOwner
from panasonic_posture.service.settings_store import SettingsStore
from panasonic_posture.session.failures import DisplayError
from panasonic_posture.session.runtime import SessionRuntime
from panasonic_posture.session.transaction import DisplayState, SoftwareDisplay
from panasonic_posture.tasks import InlineExecutor
from panasonic_posture.windows.operations import dispatch_engine_operation


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
        self.current = DisplayState(('test-internal-panel',), 0, False, True)
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
        self.state = SimpleNamespace(
            error=None,
            satisfies=lambda target: target.disable_internal_input,
        )

    def follow_hardware(self, now):
        self.running = True
        self.state = SimpleNamespace(
            error=None,
            satisfies=lambda target: target.mode == 'hardware',
        )

    def release(self):
        self.running = False
        self.state = None

    def retry(self):
        self.release()

    def request_wake(self, serial):
        raise ValueError('test input does not emit wake events')


class Simulation:
    def __init__(self, store, instance: str, scenario: str = 'laptop'):
        if scenario not in ('laptop', 'prompt', 'hardware', 'slow', 'error'):
            raise ValueError('unknown simulation scenario')
        self.now = time.monotonic() - 4
        self.scenario = scenario
        self.vector = (0., 0., -9.81)
        self.sensor = SimpleNamespace(
            info=SensorInfo(ready=True, backend='windows-simulation'),
            reading=lambda: SensorReading(self.vector, self.now),
        )
        self.actuator = SimulatedInput()
        self.owner = SessionOwner('windows-simulation', instance)
        self.engine = Engine(
            controller=Controller(),
            sensor=self.sensor,
            actuator=self.actuator,
            presentation=PosePresentation(),
            settings=store,
            instance=instance,
            device=DeviceInfo(platform='windows-simulation', support_tier='compatible'),
            hardware_query=lambda: (scenario == 'hardware', HardwareStatus.OK),
            monotonic=lambda: self.now,
            executor=InlineExecutor(),
        )
        self.display = SimulatedDisplay()
        self.session = SessionRuntime(
            SimpleNamespace(
                get_state=lambda: self.engine.state,
                session_update=lambda *args: self.engine.session_update(
                    *args, owner=self.owner,
                ),
            ),
            self.display,
            None,
            executor=InlineExecutor(),
            lease_executor=InlineExecutor(),
        )
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
            self.engine.session_update(
                self.engine.state.generation,
                False,
                owner=self.owner,
            )
        else:
            self.session.tick(now)
        self.engine.tick()

    def call(self, method: str, params: dict):
        dispatch_engine_operation(
            self.engine,
            method,
            params,
            before_retry=lambda: setattr(self.display, 'fail', False),
        )
        self.engine.tick()

    def close(self):
        self.session.close()
        self.engine.shutdown()


class BackendError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Backend:
    """Test wrapper around the shared screen pipeline."""

    def __init__(self, path, *, simulate: bool = False, scenario: str = 'laptop',
                 screen_factory=None):
        self.store = SettingsStore(path)
        self.instance = uuid.uuid4().hex
        self.simulation = None
        self.screen = None
        if simulate:
            self.simulation = Simulation(self.store, self.instance, scenario)
        if screen_factory is not None:
            journal = path.with_suffix('.display.json')
            self.screen = screen_factory(self.store, self.instance, journal)

    def advance(self, now: float) -> None:
        if self.simulation:
            self.simulation.advance(now)
        if self.screen:
            self.screen.advance(now)

    def close(self) -> None:
        if self.simulation:
            self.simulation.close()
        if self.screen:
            self.screen.close()

    def snapshot(self) -> dict:
        if self.screen:
            state = self.screen.engine.state.to_dict()
            state.update(
                preferences_available=True,
                input_control_available=False,
                available_modes=list(self.screen.available_modes),
            )
            return state
        if self.simulation:
            state = self.simulation.engine.state.to_dict()
            state.update(
                instance=self.instance,
                preferences_available=True,
                sensor_source='windows-simulation',
                support_tier='compatible',
                backend='development',
            )
            return state
        return StateSnapshot(
            instance=self.instance,
            supported=False,
            support_tier='unsupported',
            backend='unsupported',
            sensor_source='windows-unavailable',
            session_connected=True,
        ).to_dict()

    def call(self, method: str, params: dict) -> None:
        target = self.screen or self.simulation
        if target is None:
            raise BackendError(ErrorCode.UNSUPPORTED_MODEL)
        try:
            target.call(method, params)
        except ServiceError as error:
            raise BackendError(error.code) from error


__all__ = [
    'Backend',
    'BackendError',
    'MemoryJournal',
    'SimulatedDisplay',
    'SimulatedInput',
    'Simulation',
]
