"""Bounded UTF-8 JSON frames shared by Windows local transports.

Linux D-Bus and relay contracts stay unchanged. No network listener or
caller-supplied executable is exposed.
"""
import json

VERSION = 1
MAX_FRAME = 65536
MAX_PENDING = 32


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
