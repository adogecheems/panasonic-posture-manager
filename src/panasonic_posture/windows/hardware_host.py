"""Privileged-host dispatch, isolated from the ordinary preview subprocess.

Only an OS-authenticated foreground console user may send UI operations.
Display receipts additionally require the exact session process registered by
the service host, never a PID/SID supplied in an RPC request. Before the first
physical-source observation, the registered process may renew only a not-ready
heartbeat; admission loss after a healthy observation revokes it. This module
has no device discovery, installation or arbitrary device-write operation.
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
        self._admission_open = False
        self._admission_lost = False

    def _observe_admission(self) -> bool:
        admitted = bool(self.session_admission())
        if admitted:
            self._admission_open = True
            self._admission_lost = False
        elif self._admission_open:
            # The physical source was healthy and has now disappeared. A
            # dormant first-run source is different: it never authorized a
            # ready receipt, so its not-ready heartbeat may keep the desktop
            # agent discoverable without allowing any device write.
            self._admission_open = False
            self._admission_lost = True
        return admitted

    def dispatch(self, peer: ConsoleIdentity, request: dict) -> dict:
        if not isinstance(peer, ConsoleIdentity) or not self.authorize(peer):
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        method, params = validate_request(request, self.instance)
        accepted = True
        if method == 'session_update':
            if peer != self.session_process:
                raise ServiceError(ErrorCode.ACCESS_DENIED, 'unregistered-session-process')
            if not self._observe_admission():
                if self._admission_lost:
                    raise ServiceError(ErrorCode.DISPLAY_UNAVAILABLE,
                                       'physical-switch-lost')
                if params['ready']:
                    raise ServiceError(ErrorCode.DISPLAY_UNAVAILABLE,
                                       'physical-switch-not-observed')
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
                    error_detail=None,
                    hardware_release_pose=self.engine.hardware_release_pose)

    def tick(self) -> None:
        admitted = self._observe_admission()
        if self.session_process is not None and (
                not self.authorize(self.session_process) or
                (self._admission_lost and not admitted)):
            self.engine.revoke_session(self.session_process.owner)
        self.engine.tick()

    def disconnected(self, peer: ConsoleIdentity) -> None:
        if peer == self.session_process:
            self.engine.revoke_session(peer.owner)

    def close(self) -> None:
        self.engine.shutdown()
