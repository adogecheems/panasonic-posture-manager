"""Bounded UTF-8 JSON frames for a parent and its private child process.

This is a new, Windows-only transport. Linux D-Bus and relay contracts stay
unchanged. No network listener, shared pipe name or caller-supplied executable
is exposed. The inherited standard pipes are the communication capability.
"""
import json

from ..snapshot_codec import parse_snapshot

VERSION = 1
MAX_FRAME = 65536
MAX_PENDING = 32
STARTUP_REASONS = frozenset({
    'backend-startup-failed',
    'native-session-unavailable',
    'native-startup-failed',
    'slate-configuration-required',
})


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


def validate_state(value: object, *, native: bool = False) -> dict:
    if not isinstance(value, dict):
        raise ProtocolError('state must be an object')
    # These are complete snapshots, unlike the legacy Linux decoder's optional
    # fields. Missing data must never enable an action by way of a default.
    from ..model import StateSnapshot
    if not set(StateSnapshot().to_dict()) <= value.keys():
        raise ProtocolError('incomplete state')
    if type(value.get('version')) is not int:
        raise ProtocolError('invalid state version')
    try:
        state = parse_snapshot(json.dumps(value, allow_nan=False))
    except (ValueError, TypeError, RecursionError) as error:
        raise ProtocolError('invalid state fields') from error
    sources = ('windows-winrt',) if native else ('windows-unavailable', 'windows-simulation')
    if not state.instance or state.sensor_source not in sources:
        raise ProtocolError('invalid Windows state identity or source')
    if type(value.get('preferences_available')) is not bool:
        raise ProtocolError('invalid preference capability')
    if type(value.get('simulation')) is not bool:
        raise ProtocolError('invalid simulation capability')
    if value['simulation'] != (state.sensor_source == 'windows-simulation'):
        raise ProtocolError('inconsistent simulation source')
    if native:
        modes = value.get('available_modes')
        if (value.get('native_backend') is not True or value['simulation'] is not False or
                value.get('input_control_available') is not False or
                modes not in (['auto', 'laptop', 'tent', 'stand'],
                              ['auto', 'laptop', 'tent', 'stand', 'tablet']) or
                state.support_tier != 'compatible' or
                state.manual not in ('auto', 'laptop', 'tent', 'stand', 'tablet') or
                state.target.mode not in ('unavailable', 'laptop', 'tent', 'stand', 'tablet', 'hardware') or
                (state.applied is not None and state.applied.mode not in
                 ('laptop', 'tent', 'stand', 'tablet', 'hardware')) or
                state.target.disable_internal_input or
                (state.applied is not None and state.applied.disable_internal_input)):
            raise ProtocolError('invalid native screen capability')
    elif value.get('native_backend', False) is not False:
        raise ProtocolError('unexpected native backend')
    if not native and not value['simulation'] and (state.supported or state.applied is not None or state.hardware is not None):
        raise ProtocolError('unadapted hardware must not claim execution')
    result = state.to_dict()
    result.update(preferences_available=value['preferences_available'], simulation=value['simulation'])
    if native:
        result.update(native_backend=True, input_control_available=False, available_modes=list(value['available_modes']))
    return result
