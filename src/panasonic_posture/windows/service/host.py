"""Privileged-host dispatch, isolated from the ordinary UI process.

Only an OS-authenticated unlocked foreground console user may send UI operations.
The exact registered session process may read state and renew receipts while
locked; ordinary UI operations still require an unlocked console.
Display receipts additionally require the exact session process registered by
the service host, never a PID/SID supplied in an RPC request. Before the first
physical-source observation, the registered process may renew only a not-ready
heartbeat; admission loss after a healthy observation revokes it. This module
has no device discovery, installation or arbitrary device-write operation.
"""
from collections.abc import Callable
from dataclasses import replace
import logging

from panasonic_posture.model import BackendState, HardwareStatus, StateSnapshot
from panasonic_posture.contracts import ErrorCode
from panasonic_posture.service.engine import Engine, ServiceError
from panasonic_posture.windows.common.identity import ConsoleIdentity
from panasonic_posture.windows.service.registry_slate import RegistryPosture
from panasonic_posture.windows.ipc.protocol import (
    CHANNEL,
    CONTROL_VERSION,
    SIGNATURES,
    validate_request,
)
from panasonic_posture.windows.common.operations import dispatch_engine_operation


log = logging.getLogger(__name__)


class HardwareHost:
    def __init__(self, engine: Engine, *, authorize: Callable[[ConsoleIdentity], bool],
                 session_process: ConsoleIdentity | None = None,
                 session_admission: Callable[[], bool] | None = None,
                 session_authorize: Callable[[ConsoleIdentity], bool] | None = None,
                 session_locked: Callable[[], bool] | None = None,
                 registry: RegistryPosture | None = None,
                 physical_state: Callable[[], bool | None] | None = None,
                 control: Callable[[ConsoleIdentity, bool], bool] | None = None) -> None:
        self.engine, self.authorize = engine, authorize
        self.session_authorize = session_authorize or authorize
        self.session_locked = session_locked or (lambda: False)
        self._locked = False
        self.session_process = session_process
        self.session_admission = session_admission or (lambda: True)
        if registry is not None and physical_state is None:
            raise ValueError('registry control requires a live physical observation')
        self.physical_state = physical_state
        self.registry = registry
        self.registry_failed = False
        self.control = control
        self.instance = engine.state.instance
        self._admission_open = False
        self._admission_lost = False
        self._authorization_lost = False

    @property
    def state(self) -> StateSnapshot:
        state = self.engine.state
        if self.registry_failed:
            return replace(state, applied=None, backend=BackendState.ERROR,
                           error_code=ErrorCode.NATIVE_RECOVERY, error_detail='registry-posture')
        return state

    def _registry_update(self, *, release: bool = False) -> None:
        if self.registry is None or self.physical_state is None:
            return
        state = self.engine.state
        physical = self.physical_state()
        owns_target = (not release and physical is False and state.hardware is False
                       and state.hardware_status == HardwareStatus.OK and state.supported
                       and not state.sensor_error and state.session_connected
                       and self.session_process is not None
                       and self.session_authorize(self.session_process)
                       and self.engine.session_owner == self.session_process.owner
                       and state.target.mode in ('laptop', 'tent', 'stand')
                       and state.error_detail != 'native-session-unavailable')
        try:
            if not owns_target:
                self.registry.release(physical)
            elif state.target.mode in ('tent', 'stand'):
                self.registry.apply(True)
            elif state.applied == state.target:
                # Laptop follows the display receipt. Tent/stand are published
                # before their generation is returned to the display worker.
                self.registry.apply(False)
            self.registry_failed = False
        except (OSError, ValueError):
            if not self.registry_failed:
                log.warning('registry posture update failed', exc_info=True)
            self.registry_failed = True
            if owns_target:
                # A failed verification can follow a successful write. Undo it
                # if possible, consulting the physical source again after I/O.
                try:
                    self.registry.release(self.physical_state())
                except (OSError, ValueError):
                    # The journal remains available for the next recovery.
                    return

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
            self._locked = False
            return True
        authorized = self.session_authorize(process)
        locked = authorized and self.session_locked()
        if locked and not self._locked and self.engine.state.supported:
            self.engine.set_mode('auto')
        self._locked = locked
        if not authorized and not self._authorization_lost:
            # A peer disconnect can clear the owner before the next host tick
            # observes qualification loss. Reset even if ownership is gone.
            self._authorization_lost = True
            self.engine.reset_after_session_context_loss()
        elif authorized:
            self._authorization_lost = False
        return authorized

    def recovery_state(self, peer: ConsoleIdentity, request: dict) -> dict:
        """Read only, after OS authentication of the exact registered process.

        Recovery still needs a fresh physical gate when display qualification
        is lost. This path never renews a lease or changes the engine.
        """
        if not isinstance(peer, ConsoleIdentity) or peer != self.session_process:
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        method, _ = validate_request(request, self.instance)
        if method != 'get_state':
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        return dict(version=CONTROL_VERSION, channel=CHANNEL, type='response',
                    id=request['id'], instance=self.instance, accepted=True,
                    state=self.state.to_dict(), error_detail=None,
                    hardware_release_pose=self.engine.hardware_release_pose)

    def dispatch(self, peer: ConsoleIdentity, request: dict) -> dict:
        if not isinstance(peer, ConsoleIdentity):
            raise ServiceError(ErrorCode.ACCESS_DENIED)
        method, params = validate_request(request, self.instance)
        authorize = (self.session_authorize if peer == self.session_process and
                     method in ('get_state', 'session_update') else self.authorize)
        if not authorize(peer):
            raise ServiceError(ErrorCode.ACCESS_DENIED)
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
                unattended=params['unattended'] or self.session_locked())
        elif method != 'get_state':
            dispatch_engine_operation(self.engine, method, params)
        if method != 'get_state':
            self.engine.tick()
            self._registry_update()
            if self.registry_failed and (method == 'stop_control' or
                    method == 'session_update' and params['ready']):
                raise ServiceError(ErrorCode.NATIVE_RECOVERY, 'registry-posture')
        return dict(version=CONTROL_VERSION, channel=CHANNEL, type='response',
                    id=request['id'], instance=self.instance, accepted=accepted,
                    state=self.state.to_dict(),
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
        self._registry_update()

    def disconnected(self, peer: ConsoleIdentity) -> None:
        if peer == self.session_process:
            self.engine.revoke_session(peer.owner)
            self._registry_update(release=True)

    def close(self) -> None:
        self._registry_update(release=True)
        self.engine.shutdown()
        if self.registry_failed:
            raise OSError('registry posture recovery pending')
