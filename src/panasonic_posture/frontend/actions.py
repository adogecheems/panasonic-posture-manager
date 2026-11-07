"""Owner-loop interaction state with no widget, transport or scheduler imports."""
from concurrent.futures import Future
from typing import Callable

from .ports import UIClient, PREFERENCES, Preference


def _invoke(action: Callable[[], Future]) -> Future:
    # Both synchronous transport errors and failed futures take the same path.
    try:
        return action()
    except Exception as error:
        result = Future()
        result.set_exception(error)
        return result


class ActionRunner:
    """Deduplicate actions and discard completions from a previous connection."""

    def __init__(self, changed: Callable[[], None],
                 failed: Callable[[BaseException], None],
                 succeeded: Callable[[], None] = lambda: None) -> None:
        self.busy: str | None = None
        self.connection = 0
        self._changed, self._failed, self._succeeded = changed, failed, succeeded

    def disconnect(self) -> None:
        self.connection += 1
        self.busy = None

    def run(self, name: str, action: Callable[[], Future]) -> bool:
        if self.busy is not None:
            return False
        self.busy = name
        connection = self.connection
        self._changed()

        def finished(result: Future) -> None:
            if connection != self.connection:
                return
            self.busy = None
            try:
                result.result()
            except Exception as error:
                self._failed(error)
            else:
                self._succeeded()
            finally:
                self._changed()

        _invoke(action).add_done_callback(finished)
        return True


class SettingsModel:
    """Coalesce each setting independently and retain the latest service state."""

    def __init__(self, client: UIClient, changed: Callable[[], None],
                 failed: Callable[[BaseException], None]) -> None:
        self.client = client
        self.state: dict | None = None
        self.pending: dict = {}
        self.active: set[str] = set()
        self.connection = 0
        self._changed, self._failed = changed, failed

    def update(self, state: dict) -> None:
        self.state = state

    def disconnect(self) -> None:
        self.connection += 1
        self.state = None
        self.pending.clear()
        self.active.clear()

    def value(self, field: str):
        return self.pending.get(field, self.state[field] if self.state else None)

    def preference_enabled(self, field: Preference) -> bool:
        return bool(self.state and self.state['supported']
                    and (field != 'suspend_touch_wake' or self.value(field)
                         or self.state.get('suspend_touch_wake_available', False))
                    and (field != 'tent_prompt' or self.value('tent_enabled')))

    def mode_enabled(self, mode: str) -> bool:
        return bool(self.state and self.state['supported']
                    and self.state['hardware'] is not True)

    def queue(self, field: str, value: str | bool) -> None:
        if field != 'manual' and field not in PREFERENCES:
            raise ValueError(f'unknown setting: {field}')
        if self.state is None:
            return
        self.pending[field] = value
        if field not in self.active:
            self.active.add(field)
            self._write(field)

    def _write(self, field: str) -> None:
        requested = self.pending[field]
        connection = self.connection

        def finished(result: Future) -> None:
            if connection != self.connection:
                return
            try:
                result.result()
            except Exception as caught:
                error = caught
            else:
                error = None
            if self.pending.get(field) != requested:
                self._write(field)
                return
            self.pending.pop(field, None)
            self.active.discard(field)
            if error:
                self._failed(error)
                self._changed()
            self.client.refresh()

        action = (lambda: self.client.set_mode(requested)) if field == 'manual' else (
            lambda: self.client.set_preference(field, requested))
        _invoke(action).add_done_callback(finished)
