"""Strict Linux state-v2 decoding, separate from the internal model."""

from .model import (StateSnapshot, Target, Pose, HardwareStatus, Mode, BackendState, loads_state)
from .contracts import PROMPTS, SUPPORT_TIERS
from .linux_contract import SensorSource, SENSOR_SOURCES


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
    touch_wake = data.get("touch_wake")
    if touch_wake is not None and (
        type(touch_wake) is not int or not 0 < touch_wake < 2**64
    ):
        raise ValueError("touch_wake must be a positive uint64 or null")
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
        suspend_touch_wake=boolean("suspend_touch_wake", False),
        suspend_touch_wake_available=boolean("suspend_touch_wake_available", False),
        tent_enabled=boolean("tent_enabled", True),
        tent_prompt=boolean("tent_prompt", True),
        stand_prompt=boolean("stand_prompt", False),
        target=Target.from_dict(raw_target),
        applied=Target.from_dict(raw_applied) if raw_applied is not None else None,
        pending_notification=pending_notification,
        pending_prompt=raw_prompt,
        touch_wake=touch_wake,
        backend=BackendState(data.get("backend", "ready")),
        error_code=optional_string("error_code"),
        error_detail=optional_string("error_detail"),
        session_connected=boolean("session_connected", False),
        supported=boolean("supported", support_tier != "unsupported"),
        support_tier=support_tier,
        sensor_source=SensorSource(data.get("sensor_source", "starting")),
        sensor_error=optional_string("sensor_error"),
        posture_pending=boolean("posture_pending", False),
        sensor_calibrated=boolean("sensor_calibrated", False),
        profile=string("profile", "generic"),
        model=string("model", ""),
        vendor=string("vendor", ""),
    )


def ensure_sensor_source(value: str) -> SensorSource:
    if value not in SENSOR_SOURCES:
        return SensorSource.STARTING
    return SensorSource(value)
