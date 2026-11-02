"""Data model shared by the service, session adapter and tests.

The model is intentionally free of side effects so every subsystem can be
tested with plain objects and JSON dictionaries.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .protocol import SENSOR_SOURCES, STATE_VERSION


class Mode(StrEnum):
    AUTO = "auto"
    LAPTOP = "laptop"
    TENT = "tent"
    STAND = "stand"
    TABLET = "tablet"


class Pose(StrEnum):
    LAPTOP = "laptop"
    TENT = "tent"
    STAND = "stand"
    TABLET = "tablet"
    UNKNOWN = "unknown"


class HardwareStatus(StrEnum):
    OK = "ok"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"
    PERMISSION = "permission"
    READ_ERROR = "read_error"


class SensorSource(StrEnum):
    STARTING = "starting"
    IIO = "iio"
    SYSFS = "sysfs"


class BackendState(StrEnum):
    READY = "ready"
    ERROR = "error"
    DEVELOPMENT = "development"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Target:
    """What the service wants the backend to apply.

    ``tablet`` is ``None`` when the target is unavailable/native (no software
    takeover requested), otherwise it is the aggregated tablet status the
    relay must expose.  ``rotation`` is ``None`` when native rotation policy is
    allowed (hardware tablet) and 0/180 for the locked software modes.
    """

    mode: str = "unavailable"
    tablet: bool | None = None
    rotation: int | None = None
    disable_internal_input: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "tablet": self.tablet,
            "rotation": self.rotation,
            "disable_internal_input": self.disable_internal_input,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Target":
        if not isinstance(data, dict):
            raise ValueError("target must be an object")
        tablet = data.get("tablet")
        rotation = data.get("rotation")
        return cls(
            mode=str(data.get("mode") or "unavailable"),
            tablet=None if tablet is None else bool(tablet),
            rotation=None if rotation is None else int(rotation),
            disable_internal_input=bool(data.get("disable_internal_input")),
        )


@dataclass(frozen=True)
class SensorReading:
    """Latest accelerometer sample (normalised in the profile mount frame)."""

    vector: tuple[float, float, float] | None = None
    stamp: float | None = None
    source: SensorSource = SensorSource.STARTING

    @property
    def valid(self) -> bool:
        if self.vector is None or self.stamp is None or len(self.vector) != 3:
            return False
        return all(math.isfinite(value) for value in self.vector)

    @property
    def magnitude(self) -> float:
        if not self.valid:
            return 0.0
        return math.sqrt(sum(value * value for value in self.vector or ()))


@dataclass
class StateSnapshot:
    """Serializable public state returned by GetState/StateChanged."""

    instance: str = ""
    generation: int = 0
    pose: Pose = Pose.UNKNOWN
    hardware: bool | None = None
    hardware_status: HardwareStatus = HardwareStatus.ABSENT
    manual: Mode = Mode.AUTO
    tent_prompt: bool = True
    target: Target = field(default_factory=Target)
    applied: Target | None = None
    pending_notification: int | None = None
    backend: BackendState = BackendState.READY
    error_code: str | None = None
    error_detail: str | None = None
    session_connected: bool = False
    supported: bool = True
    sensor_source: SensorSource = SensorSource.STARTING
    sensor_error: str | None = None
    profile: str = "generic"
    model: str = ""
    vendor: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "instance": self.instance,
            "generation": self.generation,
            "pose": str(self.pose),
            "hardware": self.hardware,
            "hardware_status": str(self.hardware_status),
            "manual": str(self.manual),
            "tent_prompt": self.tent_prompt,
            "target": self.target.to_dict(),
            "applied": self.applied.to_dict() if self.applied else None,
            "pending_notification": self.pending_notification,
            "backend": str(self.backend),
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "session_connected": self.session_connected,
            "supported": self.supported,
            "sensor_source": str(self.sensor_source),
            "sensor_error": self.sensor_error,
            "profile": self.profile,
            "model": self.model,
            "vendor": self.vendor,
        }


def dumps_state(state: StateSnapshot | dict[str, Any]) -> str:
    data = state.to_dict() if isinstance(state, StateSnapshot) else state
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def loads_state(text: str) -> dict[str, Any]:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("state must be a JSON object")
    version = data.get("version")
    if version != STATE_VERSION:
        raise ValueError(f"unsupported state version {version!r}")
    return data


def parse_state_snapshot(text: str) -> StateSnapshot:
    """Strict parser used by the session adapter and tests."""
    data = loads_state(text)
    return StateSnapshot(
        instance=str(data.get("instance") or ""),
        generation=int(data.get("generation") or 0),
        pose=Pose(data.get("pose", "unknown")),
        hardware=data.get("hardware"),
        hardware_status=HardwareStatus(data.get("hardware_status", "absent")),
        manual=Mode(data.get("manual", "auto")),
        tent_prompt=bool(data.get("tent_prompt", True)),
        target=Target.from_dict(data.get("target") or {}),
        applied=Target.from_dict(data["applied"]) if data.get("applied") else None,
        pending_notification=data.get("pending_notification"),
        backend=BackendState(data.get("backend", "ready")),
        error_code=data.get("error_code"),
        error_detail=data.get("error_detail"),
        session_connected=bool(data.get("session_connected", False)),
        supported=bool(data.get("supported", True)),
        sensor_source=SensorSource(data.get("sensor_source", "starting")),
        sensor_error=data.get("sensor_error"),
        profile=str(data.get("profile") or "generic"),
        model=str(data.get("model") or ""),
        vendor=str(data.get("vendor") or ""),
    )


def ensure_sensor_source(value: str) -> SensorSource:
    if value not in SENSOR_SOURCES:
        return SensorSource.STARTING
    return SensorSource(value)
