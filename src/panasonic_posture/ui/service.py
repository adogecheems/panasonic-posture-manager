"""Asynchronous system-bus client. All completions run on the GTK main loop."""
from typing import Callable
import json
import logging
from concurrent.futures import Future
from gi.repository import Gio, GLib
from ..linux_contract import NAME as BUS_NAME, PATH as OBJECT_PATH, IFACE as INTERFACE
from ..state_codec import parse_state_snapshot
from ..frontend.ports import Preference
from .errors import ServiceUnavailable, unknown_method

LOG = logging.getLogger(__name__)


def parse_state(text: str) -> dict:
    # Use the same strict decoder as the session adapter. Keep a dictionary
    # view for widgets, including defaults for older compatible snapshots.
    raw = json.loads(text)
    if not isinstance(raw, dict) or type(raw.get('generation')) is not int:
        raise ValueError('state must contain an integer generation')
    if not isinstance(raw.get('target'), dict):
        raise ValueError('state must contain a target')
    return parse_state_snapshot(text).to_dict()


class PostureService:
    def __init__(self, bus: Gio.DBusConnection | None = None) -> None:
        self._bus = bus
        self._proxy = None
        self._handlers = []
        self._owner = None
        self._creating = False
        self._retry_id = 0
        self._refresh_pending = False
        self._closed = False
        self._on_state = None
        self._on_unavailable = None
        self._on_connected = None

    def start(self, on_state: Callable[[dict], None],
              on_unavailable: Callable[[], None],
              on_connected: Callable[[], None] | None = None) -> None:
        self._on_state, self._on_unavailable = on_state, on_unavailable
        self._on_connected = on_connected
        self._connect()

    def stop(self) -> None:
        self._closed = True
        if self._retry_id:
            GLib.source_remove(self._retry_id)
            self._retry_id = 0
        self._drop_proxy()

    def _notify(self, callback, *args):
        if callback:
            try:
                callback(*args)
            except Exception:
                LOG.exception('UI callback failed')

    def _emit_state(self, text):
        try:
            state = parse_state(text)
        except (ValueError, TypeError, KeyError):
            LOG.exception('Invalid service state')
            return
        self._notify(self._on_state, state)

    def refresh(self) -> None:
        if self._closed or self._refresh_pending:
            return
        if self._proxy is None:
            self._connect()
            return
        proxy = self._proxy
        self._refresh_pending = True

        def finished(source, result):
            try:
                reply = source.call_finish(result)
            except GLib.Error:
                if proxy is self._proxy and not self._closed:
                    self._failure()
                return
            if self._closed or proxy is not self._proxy:
                return
            self._refresh_pending = False
            self._emit_state(reply.unpack()[0])

        proxy.call('GetState', None, Gio.DBusCallFlags.NONE, 5000, None, finished)

    def call(self, method: str, parameters: GLib.Variant | None = None,
             timeout_ms: int = 5000) -> Future:
        future = Future()
        proxy = self._proxy
        if self._closed or proxy is None:
            self._connect()
            future.set_exception(ServiceUnavailable())
            return future

        def finished(source, result):
            error = None
            try:
                source.call_finish(result)
            except GLib.Error as caught:
                error = caught
            if self._closed or proxy is not self._proxy:
                error = ServiceUnavailable()
            if error:
                future.set_exception(error)
            else:
                future.set_result(None)

        proxy.call(method, parameters, Gio.DBusCallFlags.NONE, timeout_ms, None, finished)
        return future

    def set_mode(self, mode: str) -> Future:
        return self.call('SetMode', GLib.Variant('(s)', (mode,)))

    def set_preference(self, field: Preference, enabled: bool) -> Future:
        methods = {'tent_enabled': 'SetTentEnabled', 'tent_prompt': 'SetTentPrompt',
                   'stand_prompt': 'SetStandPrompt'}
        return self.call(methods[field], GLib.Variant('(b)', (enabled,)))

    def confirm_prompt(self, prompt: str | None, epoch: int) -> Future:
        if prompt not in (None, 'tent', 'stand'):
            raise ValueError('unknown prompt')
        return self.call('ConfirmStand' if prompt == 'stand' else 'ConfirmTent',
                         GLib.Variant('(u)', (epoch,)))

    def calibrate(self) -> Future:
        return self.call('CalibrateSensor')

    def reset_calibration(self) -> Future:
        return self.call('ResetSensorCalibration')

    def retry(self, fallback_mode: str | None = None) -> Future:
        future = Future()

        def finished(result):
            error = result.exception()
            if error and fallback_mode and unknown_method(error):
                self.set_mode(fallback_mode).add_done_callback(complete)
            else:
                complete(result)

        def complete(result):
            error = result.exception()
            if error:
                future.set_exception(error)
            else:
                future.set_result(None)

        self.call('Retry').add_done_callback(finished)
        return future

    def _connect(self):
        if self._closed or self._creating or self._proxy is not None:
            return
        self._creating = True

        def finished(source, result):
            self._creating = False
            try:
                proxy = Gio.DBusProxy.new_finish(result) if self._bus is not None else Gio.DBusProxy.new_for_bus_finish(result)
            except GLib.Error:
                if not self._closed:
                    self._failure()
                return
            if self._closed:
                return
            self._proxy = proxy
            self._handlers = [
                proxy.connect('g-signal', self._signal),
                proxy.connect('notify::g-name-owner', self._owner_changed),
            ]
            self._owner_changed(proxy)

        args = (Gio.DBusProxyFlags.NONE, None, BUS_NAME, OBJECT_PATH, INTERFACE, None, finished)
        if self._bus is not None:
            Gio.DBusProxy.new(self._bus, *args)
        else:
            Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, *args)

    def _owner_changed(self, proxy, *args):
        if proxy is not self._proxy or self._closed:
            return
        owner = proxy.get_name_owner()
        if self._owner is not None and owner != self._owner:
            self._failure()
            return
        self._owner = owner
        if owner:
            self._notify(self._on_connected)
            self.refresh()
        else:
            self._failure()

    def _signal(self, proxy, sender, signal, parameters):
        if proxy is self._proxy and not self._closed and signal == 'StateChanged':
            try:
                self._emit_state(parameters.unpack()[0])
            except (IndexError, TypeError, ValueError):
                LOG.exception('Invalid service signal')

    def _drop_proxy(self):
        proxy, self._proxy = self._proxy, None
        self._owner = None
        for handler in self._handlers:
            proxy.disconnect(handler)
        self._handlers = []
        self._refresh_pending = False

    def _failure(self):
        self._notify(self._on_unavailable)
        self._drop_proxy()
        if not self._closed and not self._retry_id:
            self._retry_id = GLib.timeout_add(2000, self._reconnect)

    def _reconnect(self):
        self._retry_id = 0
        self._connect()
        return GLib.SOURCE_REMOVE
