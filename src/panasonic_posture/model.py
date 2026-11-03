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

from .protocol import (
    PROMPTS,
    SENSOR_SOURCES,
    STATE_VERSION,
    SUPPORT_TIERS,
    TARGET_MODES,
)


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
        mode = data.get("mode", "unavailable")
        if not isinstance(mode, str) or mode not in TARGET_MODES:
            raise ValueError(f"invalid target mode {mode!r}")
        tablet = data.get("tablet")
        if tablet is not None and not isinstance(tablet, bool):
            raise ValueError("target tablet must be a boolean or null")
        rotation = data.get("rotation")
        if rotation is not None and (
            not isinstance(rotation, int) or isinstance(rotation, bool)
        ):
            raise ValueError("target rotation must be an integer or null")
        disable_input = data.get("disable_internal_input", False)
        if not isinstance(disable_input, bool):
            raise ValueError("target disable_internal_input must be a boolean")
        return cls(
            mode=mode,
            tablet=tablet,
            rotation=rotation,
            disable_internal_input=disable_input,
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
    pose: Pose = Pose.LAPTOP
    hardware: bool | None = None
    hardware_status: HardwareStatus = HardwareStatus.ABSENT
    manual: Mode = Mode.AUTO
    tent_prompt: bool = True
    stand_prompt: bool = False
    target: Target = field(default_factory=Target)
    applied: Target | None = None
    pending_notification: int | None = None
    pending_prompt: str | None = None
    backend: BackendState = BackendState.READY
    error_code: str | None = None
    error_detail: str | None = None
    session_connected: bool = False
    supported: bool = True
    support_tier: str = "compatible"
    sensor_source: SensorSource = SensorSource.STARTING
    sensor_error: str | None = None
    sensor_calibrated: bool = False
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
            "stand_prompt": self.stand_prompt,
            "target": self.target.to_dict(),
            "applied": self.applied.to_dict() if self.applied else None,
            "pending_notification": self.pending_notification,
            "pending_prompt": self.pending_prompt,
            "backend": str(self.backend),
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "session_connected": self.session_connected,
            "supported": self.supported,
            "support_tier": self.support_tier,
            "sensor_source": str(self.sensor_source),
            "sensor_error": self.sensor_error,
            "sensor_calibrated": self.sensor_calibrated,
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

    def boolean(key: str, default: bool) -> bool:
        value = data.get(key, default)
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        return value

    def optional_boolean(key: str) -> bool | None:
        value = data.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean or null")
        return value

    def integer(key: str, default: int) -> int:
        value = data.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{key} must be a non-negative integer")
        return value

    def optional_string(key: str) -> str | None:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{key} must be a string or null")
        return value

    def string(key: str, default: str) -> str:
        value = data.get(key, default)
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return value

    raw_support_tier = data.get("support_tier")
    if raw_support_tier is None:
        support_tier = "unsupported" if data.get("supported") is False else "compatible"
    else:
        if not isinstance(raw_support_tier, str) or raw_support_tier not in SUPPORT_TIERS:
            raise ValueError(f"invalid support tier {raw_support_tier!r}")
        support_tier = raw_support_tier
    raw_prompt = data.get("pending_prompt")
    if raw_prompt is not None and raw_prompt not in PROMPTS:
        raise ValueError(f"invalid pending prompt {raw_prompt!r}")
    pending_notification = data.get("pending_notification")
    if pending_notification is not None and (
        not isinstance(pending_notification, int)
        or isinstance(pending_notification, bool)
        or pending_notification < 0
    ):
        raise ValueError("pending_notification must be a non-negative integer or null")
    raw_target = data.get("target", {})
    if not isinstance(raw_target, dict):
        raise ValueError("target must be an object")
    raw_applied = data.get("applied")
    if raw_applied is not None and not isinstance(raw_applied, dict):
        raise ValueError("applied must be an object or null")
    return StateSnapshot(
        instance=string("instance", ""),
        generation=integer("generation", 0),
        pose=Pose(data.get("pose", "laptop")),
        hardware=optional_boolean("hardware"),
        hardware_status=HardwareStatus(data.get("hardware_status", "absent")),
        manual=Mode(data.get("manual", "auto")),
        tent_prompt=boolean("tent_prompt", True),
        stand_prompt=boolean("stand_prompt", False),
        target=Target.from_dict(raw_target),
        applied=Target.from_dict(raw_applied) if raw_applied is not None else None,
        pending_notification=pending_notification,
        pending_prompt=raw_prompt,
        backend=BackendState(data.get("backend", "ready")),
        error_code=optional_string("error_code"),
        error_detail=optional_string("error_detail"),
        session_connected=boolean("session_connected", False),
        supported=boolean("supported", support_tier != "unsupported"),
        support_tier=support_tier,
        sensor_source=SensorSource(data.get("sensor_source", "starting")),
        sensor_error=optional_string("sensor_error"),
        sensor_calibrated=boolean("sensor_calibrated", False),
        profile=string("profile", "generic"),
        model=string("model", ""),
        vendor=string("vendor", ""),
    )


def ensure_sensor_source(value: str) -> SensorSource:
    if value not in SENSOR_SOURCES:
        return SensorSource.STARTING
    return SensorSource(value)


def ensure_support_tier(value: str) -> str:
    return value if value in SUPPORT_TIERS else "compatible"
