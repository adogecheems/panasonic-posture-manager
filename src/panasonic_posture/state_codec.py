"""Strict Linux state-v2 decoding, separate from the internal model."""
from .model import StateSnapshot
from .snapshot_codec import parse_snapshot
from .linux_contract import SensorSource, SENSOR_SOURCES


def parse_state_snapshot(text: str) -> StateSnapshot:
    state = parse_snapshot(text)
    state.sensor_source = SensorSource(state.sensor_source)
    return state


def ensure_sensor_source(value: str) -> SensorSource:
    if value not in SENSOR_SOURCES:
        return SensorSource.STARTING
    return SensorSource(value)
