"""Bounded UTF-8 JSON frames for a parent and its private child process.

This is a new, Windows-only transport. Linux D-Bus and relay contracts stay
unchanged. No network listener, shared pipe name or caller-supplied executable
is exposed. The inherited standard pipes are the communication capability.
"""
import json

from .state_contract import parse_complete_snapshot

VERSION = 1
MAX_FRAME = 65536
MAX_PENDING = 32
STARTUP_REASONS = frozenset({'backend-startup-failed'})


class ProtocolError(ValueError):
    pass


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError('duplicate JSON key')
        result[key] = value
    return result


def _constant(value):
    raise ProtocolError('non-finite JSON number')


def encode(value: dict) -> bytes:
    try:
        frame = (json.dumps(value, ensure_ascii=False, allow_nan=False,
                            separators=(',', ':')) + '\n').encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise ProtocolError('invalid frame') from error
    if len(frame) > MAX_FRAME:
        raise ProtocolError('frame too large')
    return frame


def encode_startup_failure(reason: str) -> bytes:
    if reason not in STARTUP_REASONS:
        raise ProtocolError('unknown startup failure')
    return encode(dict(version=VERSION, type='fatal', reason=reason))


def decode(frame: bytes) -> dict:
    if len(frame) > MAX_FRAME or not frame.endswith(b'\n'):
        raise ProtocolError('invalid frame length or delimiter')
    try:
        value = json.loads(frame.decode('utf-8'), object_pairs_hook=_object,
                           parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError('invalid JSON frame') from error
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != VERSION:
        raise ProtocolError('unsupported transport version')
    return value


def validate_state(value: object) -> dict:
    # These are complete snapshots, unlike the legacy Linux decoder's optional
    # fields. Missing data must never enable an action by way of a default.
    try:
        state = parse_complete_snapshot(value)
    except ValueError as error:
        raise ProtocolError(str(error)) from error
    sources = ('windows-unavailable', 'windows-simulation')
    if not state.instance or state.sensor_source not in sources:
        raise ProtocolError('invalid Windows state identity or source')
    if type(value.get('preferences_available')) is not bool:
        raise ProtocolError('invalid preference capability')
    if type(value.get('simulation')) is not bool:
        raise ProtocolError('invalid simulation capability')
    if value['simulation'] != (state.sensor_source == 'windows-simulation'):
        raise ProtocolError('inconsistent simulation source')
    if value.get('native_backend', False) is not False:
        raise ProtocolError('unexpected native backend')
    if not value['simulation'] and (state.supported or state.applied is not None or state.hardware is not None):
        raise ProtocolError('unadapted hardware must not claim execution')
    result = state.to_dict()
    result.update(preferences_available=value['preferences_available'], simulation=value['simulation'])
    return result
