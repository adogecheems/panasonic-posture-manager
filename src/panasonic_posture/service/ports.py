"""Platform services used by the posture engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..model import SensorReading, SensorInfo, Target
from ..contracts import error_message


class SensorBackend(Protocol):
    """Supply normalized samples stamped in the engine's sample clock."""

    @property
    def info(self) -> SensorInfo: ...

    def reading(self) -> SensorReading: ...


@dataclass(frozen=True)
class SessionOwner:
    """Opaque identity supplied only after platform authentication."""

    authority: str
    subject: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, str) or not self.authority:
            raise ValueError("session authority must be non-empty")
        if not isinstance(self.subject, str) or not self.subject:
            raise ValueError("session subject must be non-empty")


class InputState(Protocol):
    """Platform execution evidence, distinct from physical switch state."""

    def satisfies(self, target: Target) -> bool:
        """True only after every platform-specific target effect is confirmed."""
        ...

    @property
    def error(self) -> str | None: ...


class ActuatorError(RuntimeError):
    """A failure carrying the stable code the engine should report."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(error_message(code, detail))


class InputActuator(Protocol):
    """Maintain bounded ownership of platform input-control effects."""

    @property
    def state(self) -> InputState | None: ...

    @property
    def running(self) -> bool: ...

    @property
    def error_code(self) -> str | None: ...

    @property
    def error_detail(self) -> str | None: ...

    def keep(self, now: float) -> None: ...

    def follow_hardware(self, now: float) -> None:
        """Follow physical input control, with a bounded display handoff on exit."""
        ...

    def release(self) -> None: ...

    def retry(self) -> None: ...

    @property
    def touch_activity(self) -> TouchActivity | None: ...

    def request_wake(self, serial: int) -> None:
        """Queue one bounded, contact-backed wake event; never renew a lease."""
        ...


@dataclass(frozen=True)
class TouchActivity:
    """Contact evidence stamped in the engine's monotonic clock domain."""

    serial: int
    observed_at: float


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
