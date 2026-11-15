"""Privileged-host dispatch, isolated from the ordinary UI process.

Only an OS-authenticated unlocked foreground console user may send UI operations.
The exact registered session process may read state while locked for recovery.
Display receipts additionally require the exact session process registered by
the service host, never a PID/SID supplied in an RPC request. Before the first
physical-source observation, the registered process may renew only a not-ready
heartbeat; admission loss after a healthy observation revokes it. This module
has no device discovery, installation or arbitrary device-write operation.
"""
from collections.abc import Callable

from panasonic_posture.contracts import ErrorCode
from panasonic_posture.service.engine import Engine, ServiceError
from panasonic_posture.windows.common.identity import ConsoleIdentity
from panasonic_posture.windows.ipc.protocol import (
    CHANNEL,
    CONTROL_VERSION,
    SIGNATURES,
    validate_request,
)
from panasonic_posture.windows.common.operations import dispatch_engine_operation


class HardwareHost:
    def __init__(self, engine: Engine, *, authorize: Callable[[ConsoleIdentity], bool],
                 session_process: ConsoleIdentity | None = None,
                 session_admission: Callable[[], bool] | None = None,
                 control: Callable[[ConsoleIdentity, bool], bool] | None = None) -> None:
        self.engine, self.authorize = engine, authorize
        self.session_process = session_process
        self.session_admission = session_admission or (lambda: True)
        self.control = control
        self.instance = engine.state.instance
        self._admission_open = False
        self._admission_lost = False
        self._authorization_lost = False

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
            self.engine.reset_after_session_context_loss()
        return admitted

    def _observe_authorization(self) -> bool:
        process = self.session_process
        if process is None:
            self._authorization_lost = False
            return True
        authorized = self.authorize(process)
        if not authorized and not self._authorization_lost:
            # A peer disconnect can clear the owner before the next host tick
            # observes the lock.  Reset even when ownership is already gone.
            self._authorization_lost = True
            self.engine.reset_after_session_context_loss()
        elif authorized:
            self._authorization_lost = False
        return authorized

    def recovery_state(self, peer: ConsoleIdentity, request: dict) -> dict:
        """Read only, after OS authentication of the exact registered process.

        Locking revokes receipts and targets, but recovery still needs a fresh
        physical gate. This path never renews a lease or changes the engine.
        """
        if not isinstance(peer, ConsoleIdentity) or peer != self.session_process:
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        method, _ = validate_request(request, self.instance)
        if method != 'get_state':
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        return dict(version=CONTROL_VERSION, channel=CHANNEL, type='response',
                    id=request['id'], instance=self.instance, accepted=True,
                    state=self.engine.state.to_dict(), error_detail=None,
                    hardware_release_pose=self.engine.hardware_release_pose)

    def dispatch(self, peer: ConsoleIdentity, request: dict) -> dict:
        if not isinstance(peer, ConsoleIdentity) or not self.authorize(peer):
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        method, params = validate_request(request, self.instance)
        accepted = True
        if method in ('start_control', 'stop_control'):
            if self.control is None or peer == self.session_process:
                raise ServiceError(ErrorCode.ACCESS_DENIED)
            try:
                accepted = self.control(peer, method == 'start_control')
                if method == 'stop_control' and accepted:
                    self.engine.reset_after_session_context_loss()
            except OSError as error:
                raise ServiceError(ErrorCode.DISPLAY_UNAVAILABLE, 'session-stop-incomplete') from error
        elif method == 'session_update':
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
        authorized = self._observe_authorization()
        admitted = self._observe_admission()
        if self.session_process is not None and (
                not authorized or
                (self._admission_lost and not admitted)):
            self.engine.revoke_session(self.session_process.owner)
        self.engine.tick()

    def disconnected(self, peer: ConsoleIdentity) -> None:
        if peer == self.session_process:
            self.engine.revoke_session(peer.owner)

    def close(self) -> None:
        self.engine.shutdown()
