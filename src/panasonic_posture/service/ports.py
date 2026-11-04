"""Platform services used by the posture engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..model import SensorReading, SensorSource
from ..protocol import error_message


class SensorBackend(Protocol):
    """Supply normalized samples stamped in the engine's sample clock."""

    @property
    def source(self) -> SensorSource: ...

    @property
    def sensor_error(self) -> str | None: ...

    def reading(self) -> SensorReading: ...


class InputState(Protocol):
    """Report the observed tablet signal and input suppression separately."""

    @property
    def tablet(self) -> bool: ...

    @property
    def input_disabled(self) -> bool: ...

    @property
    def error(self) -> str | None: ...


class ActuatorError(RuntimeError):
    """A failure carrying the stable code the engine should report."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(error_message(code, detail))


class InputActuator(Protocol):
    """Maintain bounded software ownership of tablet signaling and input."""

    @property
    def state(self) -> InputState | None: ...

    @property
    def running(self) -> bool: ...

    @property
    def error_code(self) -> str | None: ...

    @property
    def error_detail(self) -> str | None: ...

    def keep(self, now: float) -> None: ...

    def release(self) -> None: ...

    def retry(self) -> None: ...


@dataclass(frozen=True)
class Settings:
    """Posture preferences shared by service hosts."""

    tent_enabled: bool = True
    tent_prompt: bool = True
    stand_prompt: bool = False
    sensor_calibration: tuple[float, ...] | None = None


class SettingsBackend(Protocol):
    def load(self) -> Settings: ...

    def save(self, settings: Settings) -> None: ...
