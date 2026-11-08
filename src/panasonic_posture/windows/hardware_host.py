"""Privileged-host dispatch, isolated from the ordinary preview subprocess.

Only an OS-authenticated foreground console user may send UI operations.
Display receipts additionally require the exact session process registered by
the service host, never a PID/SID supplied in an RPC request. This module has
no device discovery, installation or arbitrary device-write operation.
"""
from dataclasses import dataclass
from collections.abc import Callable

from ..contracts import ALL_ERROR_CODES, ErrorCode, MODES, PROMPTS
from ..frontend.ports import PREFERENCES
from ..service.engine import Engine, ServiceError
from ..service.ports import SessionOwner
from .protocol import ProtocolError

CHANNEL = 'windows-hardware'
CONTROL_VERSION = 1


@dataclass(frozen=True)
class ConsoleIdentity:
    sid: str
    logon_id: int
    session_id: int
    process_id: int
    created_at: int

    def __post_init__(self) -> None:
        if not isinstance(self.sid, str) or not self.sid.startswith('S-1-'):
            raise ValueError('invalid Windows SID')
        if any(type(value) is not int or value <= 0 for value in (
                self.logon_id, self.session_id, self.process_id, self.created_at)):
            raise ValueError('invalid Windows process or logon identity')

    @property
    def owner(self) -> SessionOwner:
        return SessionOwner('windows-console',
            f'{self.sid}:{self.logon_id}:{self.session_id}:{self.process_id}:{self.created_at}')


SIGNATURES = {
    'get_state': (), 'set_mode': ('mode',), 'set_preference': ('field', 'enabled'),
    'retry': (), 'calibrate': (), 'reset_calibration': (),
    'confirm_prompt': ('prompt', 'epoch'),
    'session_update': ('generation', 'ready', 'error_code', 'error_detail', 'unattended'),
}


def validate_request(value: object, instance: str) -> tuple[str, dict]:
    if (not isinstance(value, dict) or set(value) != {
            'version', 'channel', 'id', 'instance', 'method', 'params'} or
            type(value['version']) is not int or value['version'] != CONTROL_VERSION or
            value['channel'] != CHANNEL or type(value['id']) is not int or
            not 0 < value['id'] < 2**63):
        raise ProtocolError('invalid hardware request envelope')
    method, params = value['method'], value['params']
    if not isinstance(method, str) or method not in SIGNATURES or not isinstance(params, dict):
        raise ProtocolError('unknown hardware method')
    if set(params) != set(SIGNATURES[method]):
        raise ProtocolError('invalid hardware parameters')
    if value['instance'] != instance and not (method == 'get_state' and value['instance'] is None):
        raise ProtocolError('stale hardware service instance')
    if method == 'set_mode' and (not isinstance(params['mode'], str) or params['mode'] not in MODES):
        raise ProtocolError('invalid mode')
    if method == 'set_preference' and (not isinstance(params['field'], str) or
            params['field'] not in PREFERENCES or type(params['enabled']) is not bool):
        raise ProtocolError('invalid preference')
    if method == 'confirm_prompt' and (params['prompt'] not in PROMPTS or
            type(params['epoch']) is not int or not 0 <= params['epoch'] < 2**63):
        raise ProtocolError('invalid confirmation')
    if method == 'session_update':
        if (type(params['generation']) is not int or not 0 <= params['generation'] < 2**63 or
                type(params['ready']) is not bool or type(params['unattended']) is not bool or
                params['error_code'] is not None and (not isinstance(params['error_code'], str) or
                                                     params['error_code'] not in ALL_ERROR_CODES) or
                params['error_detail'] is not None and (not isinstance(params['error_detail'], str) or
                                                       len(params['error_detail']) > 512) or
                params['ready'] and (params['unattended'] or params['error_code'] is not None or
                                     params['error_detail'] is not None)):
            raise ProtocolError('invalid session receipt')
    return method, params


class HardwareHost:
    def __init__(self, engine: Engine, *, authorize: Callable[[ConsoleIdentity], bool],
                 session_process: ConsoleIdentity | None = None) -> None:
        self.engine, self.authorize = engine, authorize
        self.session_process = session_process
        self.instance = engine.state.instance

    def dispatch(self, peer: ConsoleIdentity, request: dict) -> dict:
        if not isinstance(peer, ConsoleIdentity) or not self.authorize(peer):
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        method, params = validate_request(request, self.instance)
        accepted = True
        if method == 'session_update':
            if peer != self.session_process:
                raise ServiceError(ErrorCode.ACCESS_DENIED, 'unregistered-session-process')
            accepted = self.engine.session_update(params['generation'], params['ready'],
                params['error_code'], params['error_detail'], owner=peer.owner,
                unattended=params['unattended'])
        elif method == 'set_mode':
            self.engine.set_mode(params['mode'])
        elif method == 'set_preference':
            getattr(self.engine, 'set_' + params['field'])(params['enabled'])
        elif method == 'retry':
            self.engine.retry()
        elif method == 'confirm_prompt':
            getattr(self.engine, 'confirm_' + params['prompt'])(params['epoch'])
        elif method == 'calibrate':
            self.engine.calibrate_sensor()
        elif method == 'reset_calibration':
            self.engine.reset_sensor_calibration()
        if method != 'get_state':
            self.engine.tick()
        return dict(version=CONTROL_VERSION, channel=CHANNEL, type='response',
                    id=request['id'], instance=self.instance, accepted=accepted,
                    state=self.engine.state.to_dict())

    def tick(self) -> None:
        if self.session_process is not None and not self.authorize(self.session_process):
            self.engine.revoke_session(self.session_process.owner)
        self.engine.tick()

    def disconnected(self, peer: ConsoleIdentity) -> None:
        if peer == self.session_process:
            self.engine.revoke_session(peer.owner)

    def close(self) -> None:
        self.engine.shutdown()
