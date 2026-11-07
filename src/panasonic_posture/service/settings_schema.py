"""Settings v1 codec shared by platform storage adapters."""

from ..calibration import parse_calibration_matrix
from .ports import Settings

SETTINGS_VERSION = 1


def parse_settings(data: object) -> Settings:
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    version = data.get("version")
    if version != SETTINGS_VERSION:
        raise ValueError(f"unsupported settings version {version!r}")
    suspend_touch_wake = data.get("suspend_touch_wake", False)
    if not isinstance(suspend_touch_wake, bool):
        raise ValueError("suspend_touch_wake must be a boolean")
    tent_enabled = data.get("tent_enabled", True)
    if not isinstance(tent_enabled, bool):
        raise ValueError("tent_enabled must be a boolean")
    tent_prompt = data.get("tent_prompt")
    if not isinstance(tent_prompt, bool):
        raise ValueError("tent_prompt must be a boolean")
    stand_prompt = data.get("stand_prompt", False)
    if not isinstance(stand_prompt, bool):
        raise ValueError("stand_prompt must be a boolean")
    try:
        sensor_calibration = parse_calibration_matrix(
            data.get("sensor_calibration")
        )
    except ValueError as error:
        raise ValueError(f"invalid sensor_calibration: {error}") from error
    return Settings(
        suspend_touch_wake=suspend_touch_wake,
        tent_enabled=tent_enabled,
        tent_prompt=tent_prompt,
        stand_prompt=stand_prompt,
        sensor_calibration=sensor_calibration,
    )


def encode_settings(settings: Settings) -> dict:
    if not isinstance(settings, Settings):
        raise TypeError("settings must be a Settings instance")
    return {
        "version": SETTINGS_VERSION,
        "suspend_touch_wake": settings.suspend_touch_wake,
        "tent_enabled": bool(settings.tent_enabled),
        "tent_prompt": bool(settings.tent_prompt),
        "stand_prompt": bool(settings.stand_prompt),
        "sensor_calibration": (
            list(settings.sensor_calibration)
            if settings.sensor_calibration is not None
            else None
        ),
    }
