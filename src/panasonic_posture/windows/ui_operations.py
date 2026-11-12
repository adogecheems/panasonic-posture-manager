"""Shared user-operation surface for Windows UI transports."""
from concurrent.futures import Future

from ..contracts import Preference


class UIClientOperations:
    """Translate frontend operations into one transport request."""

    def _operation(self, method: str, params: dict) -> Future:
        raise NotImplementedError

    def set_mode(self, mode: str) -> Future:
        return self._operation('set_mode', {'mode': mode})

    def set_preference(self, field: Preference, enabled: bool) -> Future:
        return self._operation('set_preference', {'field': field, 'enabled': enabled})

    def confirm_prompt(self, prompt: str | None, epoch: int) -> Future:
        return self._operation('confirm_prompt', {'prompt': prompt or 'tent', 'epoch': epoch})

    def calibrate(self) -> Future:
        return self._operation('calibrate', {})

    def reset_calibration(self) -> Future:
        return self._operation('reset_calibration', {})

    def _retry(self, fallback_mode: str | None) -> Future:
        return self._operation('retry', {'fallback_mode': fallback_mode})

    def retry(self, fallback_mode: str | None = None) -> Future:
        return self._retry(fallback_mode)


__all__ = ['UIClientOperations']
