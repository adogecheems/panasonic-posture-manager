"""Complete StateSnapshot v2 decoding shared by Windows transports."""
import json

from panasonic_posture.model import StateSnapshot
from panasonic_posture.snapshot_codec import parse_snapshot


def parse_complete_snapshot(value: object, *, incomplete: str = 'incomplete state') -> StateSnapshot:
    if (not isinstance(value, dict) or not set(StateSnapshot().to_dict()) <= value.keys() or
            type(value.get('version')) is not int):
        raise ValueError(incomplete)
    try:
        return parse_snapshot(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError('invalid state fields') from error


def parse_hardware_release_pose(value: object) -> str | None:
    if value is None:
        return None
    if value in ('tent', 'stand'):
        return value
    raise ValueError('invalid hardware release pose')
