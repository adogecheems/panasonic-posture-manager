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

from .contracts import (
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


class BackendState(StrEnum):
    READY = "ready"
    ERROR = "error"
    DEVELOPMENT = "development"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Target:
    """What the service wants the backend to apply.

    ``tablet`` preserves the Linux state-v2 aggregate signal intent. Execution
    decisions use ``disable_internal_input`` and platform completion evidence;
    this field is not a physical hardware observation. ``rotation`` is ``None``
    when native rotation policy is allowed (hardware tablet) and 0/180 for the locked software modes.
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
class SensorInfo:
    """Acquisition status and diagnostic identity, independent of transport."""

    ready: bool = False
    backend: str = ""
    device: str = ""
    error: str | None = None


@dataclass(frozen=True)
class DeviceInfo:
    """Machine identity and support evidence for this platform only.

    Device selectors and mount transforms belong to acquisition adapters.
    A platform must not inherit another platform's validation tier.
    """

    id: str = "generic"
    product: str = ""
    vendor: str = ""
    support_tier: str = "compatible"
    platform: str = ""


@dataclass(frozen=True)
class SensorReading:
    """Latest accelerometer sample (normalised in the profile mount frame)."""

    vector: tuple[float, float, float] | None = None
    stamp: float | None = None
    source: str = "starting"

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
    """Internal state with a compatible Linux v2 projection via ``to_dict``.

    ``sensor_info`` retains platform diagnostics and is not part of v2.
    """

    instance: str = ""
    generation: int = 0
    pose: Pose = Pose.LAPTOP
    hardware: bool | None = None
    hardware_status: HardwareStatus = HardwareStatus.ABSENT
    manual: Mode = Mode.AUTO
    tent_enabled: bool = True
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
    sensor_source: str = "starting"
    sensor_error: str | None = None
    sensor_info: SensorInfo = field(default_factory=SensorInfo)
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
            "tent_enabled": self.tent_enabled,
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
    """Compatibility entry point for the Linux v2 state decoder."""
    from .state_codec import parse_state_snapshot as parse
    return parse(text)


def ensure_sensor_source(value: str):
    from .state_codec import ensure_sensor_source as ensure
    return ensure(value)


def __getattr__(name: str):
    # Keep existing Linux callers compatible without loading platform enums
    # when the domain model is imported by another platform.
    if name == "SensorSource":
        from .linux_contract import SensorSource
        return SensorSource
    raise AttributeError(name)


def ensure_support_tier(value: str) -> str:
    return value if value in SUPPORT_TIERS else "compatible"
