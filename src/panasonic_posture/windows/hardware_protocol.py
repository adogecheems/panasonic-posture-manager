"""Strict versioned envelope for the privileged hardware endpoint."""
from .protocol import ProtocolError
from .request_contract import HARDWARE_SIGNATURES, validate_operation

CHANNEL = 'windows-hardware'
CONTROL_VERSION = 1
SIGNATURES = HARDWARE_SIGNATURES


def validate_request(value: object, instance: str) -> tuple[str, dict]:
    if (not isinstance(value, dict) or set(value) != {
            'version', 'channel', 'id', 'instance', 'method', 'params'} or
            type(value['version']) is not int or value['version'] != CONTROL_VERSION or
            value['channel'] != CHANNEL or type(value['id']) is not int or
            not 0 < value['id'] < 2**63):
        raise ProtocolError('invalid hardware request envelope')
    method, params = value['method'], value['params']
    if value['instance'] != instance and not (method == 'get_state' and value['instance'] is None):
        raise ProtocolError('stale hardware service instance')
    try:
        return validate_operation(method, params, HARDWARE_SIGNATURES)
    except ValueError as error:
        raise ProtocolError(str(error)) from error


__all__ = ['CHANNEL', 'CONTROL_VERSION', 'SIGNATURES', 'validate_request']
