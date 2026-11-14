"""Fail-closed UI client used when no platform service is available."""
from concurrent.futures import Future


class UnavailableError(RuntimeError):
    """An operation rejected because the platform service is unavailable."""


def unavailable_result(error: str = 'unavailable') -> Future:
    future = Future()
    future.set_exception(UnavailableError(error))
    return future


class UnavailableClient:
    """Never reports success or synthesizes a connection."""

    def start(self, on_state, on_unavailable, on_connected=None):
        del on_state, on_connected
        self._unavailable = on_unavailable
        on_unavailable()

    def refresh(self):
        self._unavailable()

    def stop(self):
        pass

    def set_mode(self, mode):
        return unavailable_result()

    def set_preference(self, field, enabled):
        return unavailable_result()

    def confirm_prompt(self, prompt, epoch):
        return unavailable_result()

    def calibrate(self):
        return unavailable_result()

    def reset_calibration(self):
        return unavailable_result()

    def retry(self, fallback_mode=None):
        return unavailable_result()


__all__ = ['UnavailableClient', 'UnavailableError', 'unavailable_result']
