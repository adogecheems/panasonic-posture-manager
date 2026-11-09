"""Private user backend with explicit simulation and native screen routes."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
import uuid
from typing import TYPE_CHECKING

from ..contracts import ErrorCode
from ..frontend.ports import PREFERENCES
from ..model import StateSnapshot
from ..service.settings_store import SettingsStore

if TYPE_CHECKING:
    from .screen_session import ScreenSession


class BackendError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class Backend:
    def __init__(self, path: Path, *, simulate: bool = False, scenario: str = 'laptop',
                 native: bool = False,
                 screen_factory: Callable[[SettingsStore, str, Path], ScreenSession] | None = None) -> None:
        if native and simulate:
            raise ValueError('native and simulation backends are mutually exclusive')
        if screen_factory is not None and not native:
            raise ValueError('screen_factory requires native mode')
        self._resources = ExitStack()
        self.store = SettingsStore(path)
        self.settings = self.store.load()
        self.instance = uuid.uuid4().hex
        self.simulation = None
        self.screen = None
        if simulate:
            from .simulation import Simulation
            self.simulation = Simulation(self.store, self.instance, scenario)
        if native:
            from .screen_session import native_screen_session
            try:
                if screen_factory is None:
                    from .client import settings_path
                    from .instance import InstanceLock
                    root = settings_path().parent
                    # A different --settings file must not create a second
                    # writer for the same user's physical display journal.
                    self._resources.enter_context(InstanceLock(root / 'screen-controller'))
                    journal = root / 'screen-display.json'
                else:
                    journal = path.with_suffix('.display.json')
                self.screen = (screen_factory or native_screen_session)(self.store, self.instance, journal)
                self._resources.callback(self.screen.close)
            except BaseException:
                self._resources.close()
                raise
        self.state = StateSnapshot(instance=self.instance, supported=False,
            support_tier='unsupported', backend='unsupported',
            sensor_source='windows-unavailable', session_connected=True).to_dict()

    def advance(self, now: float) -> None:
        if self.simulation:
            self.simulation.advance(now)
        if self.screen:
            self.screen.advance(now)

    def close(self) -> None:
        if self.simulation:
            self.simulation.close()
        self._resources.close()

    def snapshot(self) -> dict:
        if self.screen:
            from .screen_session import SCREEN_MODES
            state = self.screen.engine.state.to_dict()
            state.update(preferences_available=True, simulation=False, native_backend=True,
                         input_control_available=False, available_modes=list(SCREEN_MODES))
            return state
        state = dict(self.simulation.engine.state.to_dict() if self.simulation else self.state)
        state.update(instance=self.instance, preferences_available=True, simulation=self.simulation is not None,
                     sensor_source='windows-simulation' if self.simulation else 'windows-unavailable')
        # Simulated success is scoped to this process and never a device-support claim.
        if self.simulation:
            state['support_tier'] = 'compatible'
            state['backend'] = 'development'
        if not self.simulation:
            for field in PREFERENCES:
                state[field] = getattr(self.settings, field)
            state['sensor_calibrated'] = self.settings.sensor_calibration is not None
        return state

    def call(self, method: str, params: dict) -> None:
        if not isinstance(params, dict):
            raise ValueError('params must be an object')
        signatures = {'get_state': (), 'set_preference': ('field', 'enabled'),
                      'set_mode': ('mode',), 'retry': ('fallback_mode',),
                      'confirm_prompt': ('prompt', 'epoch'), 'calibrate': (), 'reset_calibration': ()}
        if method not in signatures or set(params) != set(signatures[method]):
            raise ValueError('unknown method or parameters')
        if method == 'get_state':
            return
        if self.screen:
            from ..service.engine import ServiceError
            try:
                self.screen.call(method, params)
            except ServiceError as error:
                raise BackendError(error.code) from error
            return
        if method == 'set_preference':
            field, enabled = params['field'], params['enabled']
            if not isinstance(field, str) or field not in PREFERENCES or type(enabled) is not bool:
                raise ValueError('invalid preference')
            if self.simulation:
                self._simulate(method, params)
                return
            updated = replace(self.settings, **{field: enabled})
            try:
                self.store.save(updated)
            except OSError as error:
                raise BackendError(ErrorCode.CONFIG_WRITE) from error
            self.settings = updated
            return
        if self.simulation is None:
            raise BackendError(ErrorCode.UNSUPPORTED_MODEL)
        self._simulate(method, params)

    def _simulate(self, method: str, params: dict) -> None:
        from ..service.engine import ServiceError
        try:
            self.simulation.call(method, params)
        except ServiceError as error:
            raise BackendError(error.code) from error
