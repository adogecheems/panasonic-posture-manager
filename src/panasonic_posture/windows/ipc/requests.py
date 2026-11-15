"""Validation for the privileged Windows hardware RPC route."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from panasonic_posture.contracts import ALL_ERROR_CODES, MODES, PREFERENCES, PROMPTS


HARDWARE_SIGNATURES = {
    'get_state': (),
    'start_control': (),
    'stop_control': (),
    'set_mode': ('mode',),
    'set_preference': ('field', 'enabled'),
    'retry': (),
    'calibrate': (),
    'reset_calibration': (),
    'confirm_prompt': ('prompt', 'epoch'),
    'session_update': ('generation', 'ready', 'error_code', 'error_detail', 'unattended'),
}


def validate_operation(method: object, params: object,
                       signatures: Mapping[str, Sequence[str]]) -> tuple[str, dict]:
    if not isinstance(method, str) or method not in signatures or not isinstance(params, dict):
        raise ValueError('unknown method or parameters')
    if set(params) != set(signatures[method]):
        raise ValueError('unknown method or parameters')
    if method == 'set_mode' and (not isinstance(params['mode'], str) or params['mode'] not in MODES):
        raise ValueError('invalid mode')
    if method == 'set_preference' and (not isinstance(params['field'], str) or
            params['field'] not in PREFERENCES or type(params['enabled']) is not bool):
        raise ValueError('invalid preference')
    if method == 'retry' and 'fallback_mode' in params:
        fallback = params['fallback_mode']
        if fallback is not None and (not isinstance(fallback, str) or fallback not in MODES):
            raise ValueError('invalid fallback mode')
    if method == 'confirm_prompt' and (params['prompt'] not in PROMPTS or
            type(params['epoch']) is not int or not 0 <= params['epoch'] < 2**63):
        raise ValueError('invalid confirmation')
    if method == 'session_update':
        if (type(params['generation']) is not int or not 0 <= params['generation'] < 2**63 or
                type(params['ready']) is not bool or type(params['unattended']) is not bool or
                params['error_code'] is not None and (not isinstance(params['error_code'], str) or
                                                     params['error_code'] not in ALL_ERROR_CODES) or
                params['error_detail'] is not None and (not isinstance(params['error_detail'], str) or
                                                       len(params['error_detail']) > 512) or
                params['ready'] and (params['unattended'] or params['error_code'] is not None or
                                     params['error_detail'] is not None)):
            raise ValueError('invalid session receipt')
    return method, params
