"""Privileged-host dispatch, isolated from the ordinary preview subprocess.

Only an OS-authenticated foreground console user may send UI operations.
Display receipts additionally require the exact session process registered by
the service host, never a PID/SID supplied in an RPC request. This module has
no device discovery, installation or arbitrary device-write operation.
"""
from collections.abc import Callable

from ..contracts import ErrorCode
from ..service.engine import Engine, ServiceError
from .console_identity import ConsoleIdentity
from .hardware_protocol import CHANNEL, CONTROL_VERSION, SIGNATURES, validate_request
from .operations import dispatch_engine_operation


class HardwareHost:
    def __init__(self, engine: Engine, *, authorize: Callable[[ConsoleIdentity], bool],
                 session_process: ConsoleIdentity | None = None,
                 session_admission: Callable[[], bool] | None = None) -> None:
        self.engine, self.authorize = engine, authorize
        self.session_process = session_process
        self.session_admission = session_admission or (lambda: True)
        self.instance = engine.state.instance

    def dispatch(self, peer: ConsoleIdentity, request: dict) -> dict:
        if not isinstance(peer, ConsoleIdentity) or not self.authorize(peer):
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        method, params = validate_request(request, self.instance)
        accepted = True
        if method == 'session_update':
            if peer != self.session_process:
                raise ServiceError(ErrorCode.ACCESS_DENIED, 'unregistered-session-process')
            if not self.session_admission():
                raise ServiceError(ErrorCode.DISPLAY_UNAVAILABLE,
                                   'physical-switch-unavailable')
            accepted = self.engine.session_update(params['generation'], params['ready'],
                params['error_code'], params['error_detail'], owner=peer.owner,
                unattended=params['unattended'])
        elif method != 'get_state':
            dispatch_engine_operation(self.engine, method, params)
        if method != 'get_state':
            self.engine.tick()
        return dict(version=CONTROL_VERSION, channel=CHANNEL, type='response',
                    id=request['id'], instance=self.instance, accepted=accepted,
                    state=self.engine.state.to_dict(),
                    hardware_release_pose=self.engine.hardware_release_pose)

    def tick(self) -> None:
        if self.session_process is not None and (
                not self.authorize(self.session_process) or not self.session_admission()):
            self.engine.revoke_session(self.session_process.owner)
        self.engine.tick()

    def disconnected(self, peer: ConsoleIdentity) -> None:
        if peer == self.session_process:
            self.engine.revoke_session(peer.owner)

    def close(self) -> None:
        self.engine.shutdown()
