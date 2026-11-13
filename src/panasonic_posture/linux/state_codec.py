"""Strict Linux state-v2 decoding, separate from the internal model."""
import json

from ..model import StateSnapshot
from ..snapshot_codec import parse_snapshot_data
from .contracts import SensorSource, SENSOR_SOURCES


def parse_state_data(data: dict) -> StateSnapshot:
    state = parse_snapshot_data(data)
    state.sensor_source = SensorSource(state.sensor_source)
    return state


def parse_state_snapshot(text: str) -> StateSnapshot:
    return parse_state_data(json.loads(text))


def ensure_sensor_source(value: str) -> SensorSource:
    if value not in SENSOR_SOURCES:
        return SensorSource.STARTING
    return SensorSource(value)
