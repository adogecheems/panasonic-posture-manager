"""Explicit absence of input control for the ordinary screen-only backend.

This port never opens an input device or claims to block one. The controller
must request disable_internal_input=False. Unexpected capture/wake requests
fail instead of creating a synthetic successful driver receipt.
"""
from panasonic_posture.contracts import ErrorCode
from panasonic_posture.service.ports import ActuatorError


class DisplayOnlyInput:
    running = False
    state = None
    error_code = error_detail = touch_activity = None

    def release(self) -> None:
        """There is no input ownership to release."""

    def retry(self) -> None:
        """There is no input resource to restart."""

    def keep(self, now: float) -> None:
        raise ActuatorError(ErrorCode.RELAY_INPUT, 'input-control-unavailable')

    def follow_hardware(self, now: float) -> None:
        raise ActuatorError(ErrorCode.RELAY_INPUT, 'input-control-unavailable')

    def request_wake(self, serial: int) -> None:
        raise ActuatorError(ErrorCode.RELAY_INPUT, 'input-wake-unavailable')
