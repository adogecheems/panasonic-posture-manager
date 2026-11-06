"""Explicit in-memory preview client. Never reads or changes host devices."""
from concurrent.futures import Future
from copy import deepcopy
from typing import Callable

from ..frontend.ports import PREFERENCES
from ..model import StateSnapshot


class PreviewError(Exception):
    """An unavailable or invalid preview operation, safe for frontend mapping."""


def result(error: str | None = None) -> Future:
    future = Future()
    if error:
        future.set_exception(PreviewError(error))
    else:
        future.set_result(None)
    return future


class UnavailableClient:
    """Default until a real authenticated Windows client is implemented."""

    def start(self, on_state, on_unavailable, on_connected=None):
        self._unavailable = on_unavailable
        on_unavailable()

    def refresh(self):
        self._unavailable()

    def stop(self):
        pass

    def set_mode(self, mode):
        return result('unavailable')

    def set_preference(self, field, enabled):
        return result('unavailable')

    def confirm_prompt(self, prompt, epoch):
        return result('unavailable')

    def calibrate(self):
        return result('unavailable')

    def reset_calibration(self):
        return result('unavailable')

    def retry(self, fallback_mode=None):
        return result('unavailable')


class DemoClient:
    """Synthetic snapshots and generation-scoped completion on an injected loop."""

    def __init__(self, schedule: Callable[[int, Callable[[], None]], None]):
        self._schedule = schedule
        self._stopped = False
        self._connected = True
        self._on_state = lambda value: None
        self._on_unavailable = lambda: None
        self.state = StateSnapshot().to_dict()
        self.state.update(instance='fluent-preview', hardware=False, hardware_status='ok',
                          session_connected=True, support_tier='validated', sensor_source='preview')
        self.scenario('laptop')

    def start(self, on_state, on_unavailable, on_connected=None):
        self._on_state, self._on_unavailable = on_state, on_unavailable
        if on_connected:
            on_connected()
        self.refresh()

    def stop(self):
        self._stopped = True

    def refresh(self):
        if not self._stopped:
            if self._connected:
                self._on_state(deepcopy(self.state))
            else:
                self._on_unavailable()

    def scenario(self, name: str):
        if name not in ('laptop', 'prompt', 'hardware', 'slow', 'error', 'disconnected'):
            raise ValueError('unknown preview scenario')
        self._connected = name != 'disconnected'
        self.state['generation'] += 1
        self.state.update(manual='auto', pose='laptop', hardware=name == 'hardware',
                          error_code=None, pending_notification=None, pending_prompt=None)
        mode = 'hardware' if name == 'hardware' else 'laptop'
        self.state['target'] = {'mode': mode}
        self.state['applied'] = {'mode': mode}
        if name == 'prompt':
            self.state.update(pose='tent', pending_prompt='tent',
                              pending_notification=self.state['generation'])
        elif name in ('slow', 'error'):
            self.state.update(manual='tent', pose='tent', target={'mode': 'tent'}, applied=None)
            if name == 'error':
                self.state['error_code'] = 'internal-error'
        self.refresh()

    def set_mode(self, mode: str):
        if self._stopped or not self._connected:
            return result('unavailable')
        if mode not in ('auto', 'laptop', 'tent', 'stand', 'tablet'):
            return result('invalid-mode')
        if self.state['hardware'] or (mode == 'tent' and not self.state['tent_enabled']):
            return result('not-allowed')
        self.state['generation'] += 1
        generation = self.state['generation']
        target = mode if mode != 'auto' else 'laptop'
        self.state.update(manual=mode, target={'mode': target}, applied=None,
                          error_code=None, pending_notification=None, pending_prompt=None)
        self.refresh()

        def applied():
            if not self._stopped and self._connected and generation == self.state['generation']:
                self.state.update(applied={'mode': target}, pose=target)
                self.refresh()

        self._schedule(350, applied)
        return result()

    def set_preference(self, field, enabled):
        if self._stopped or not self._connected:
            return result('unavailable')
        if field not in PREFERENCES or type(enabled) is not bool:
            return result('invalid-preference')
        self.state[field] = enabled
        if field == 'tent_enabled' and not enabled and self.state['target']['mode'] == 'tent':
            self.set_mode('auto')
        self.refresh()
        return result()

    def confirm_prompt(self, prompt, epoch):
        if self._stopped or not self._connected:
            return result('unavailable')
        if (prompt not in ('tent', 'stand') or type(epoch) is not int
                or prompt != self.state['pending_prompt'] or epoch != self.state['pending_notification']):
            return result('stale-prompt')
        return self.set_mode(prompt)

    def calibrate(self):
        if self._stopped or not self._connected or self.state['hardware']:
            return result('not-allowed')
        self.state['sensor_calibrated'] = True
        self.refresh()
        return result()

    def reset_calibration(self):
        if self._stopped or not self._connected:
            return result('unavailable')
        self.state['sensor_calibrated'] = False
        self.refresh()
        return result()

    def retry(self, fallback_mode=None):
        return self.set_mode(fallback_mode or self.state['manual'])
