// D-Bus proxy wrapper for work.mmoe.panasonic.Posture.
//
// The proxy is deliberately disposable: it is recreated after a creation
// failure, after the service loses its bus name and after a call failure.
// Previously a failed proxy was permanent, so the window could only be fixed
// by restarting it (P3).  Incoming state documents are validated against the
// v2 schema before the pages see them.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';

import {SERVICE_UNAVAILABLE_CODE, isUnknownMethodError} from './errors.js';

export const BUS_NAME = 'work.mmoe.panasonic.Posture';
export const OBJECT_PATH = '/work/mmoe/panasonic/Posture';
export const INTERFACE = BUS_NAME;
export const STATE_VERSION = 2;

const RECONNECT_SECONDS = 2;
const CALL_TIMEOUT_MS = 5000;

export function serviceUnavailableError() {
    const error = new Error('background service unavailable');
    error.code = SERVICE_UNAVAILABLE_CODE;
    return error;
}

export function parseState(text) {
    const state = JSON.parse(text);
    if (state === null || typeof state !== 'object' || Array.isArray(state))
        throw new Error('state document must be a JSON object');
    if (state.version !== STATE_VERSION)
        throw new Error(`unsupported state version ${String(state.version)}`);
    if (!Number.isInteger(state.generation))
        throw new Error('state document has no generation');
    if (state.target === null || typeof state.target !== 'object')
        throw new Error('state document has no target');
    return state;
}

export class PostureService {
    constructor(bus = Gio.DBus.system) {
        this._bus = bus;
        this._proxy = null;
        this._creating = false;
        this._retryId = 0;
        this._refreshPending = false;
        this._closed = false;
        this._onState = null;
        this._onUnavailable = null;
        this._onConnected = null;
    }

    start({onState, onUnavailable, onConnected = null}) {
        this._onState = onState;
        this._onUnavailable = onUnavailable;
        this._onConnected = onConnected;
        this._connect();
    }

    stop() {
        this._closed = true;
        if (this._retryId) {
            GLib.source_remove(this._retryId);
            this._retryId = 0;
        }
        this._proxy = null;
    }

    refresh() {
        if (this._refreshPending)
            return;
        if (!this._proxy) {
            this._connect();
            return;
        }
        const proxy = this._proxy;
        this._refreshPending = true;
        proxy.call('GetState', null, Gio.DBusCallFlags.NONE, CALL_TIMEOUT_MS, null,
            (source, result) => {
                this._refreshPending = false;
                if (this._closed)
                    return;
                let text;
                try {
                    text = source.call_finish(result).deepUnpack()[0];
                } catch (error) {
                    this._handleFailure(error);
                    return;
                }
                this._emitState(text);
            });
    }

    call(method, parameters = null, timeoutMs = CALL_TIMEOUT_MS) {
        const proxy = this._proxy;
        if (!proxy) {
            this._connect();
            return Promise.reject(serviceUnavailableError());
        }
        return new Promise((resolve, reject) => {
            proxy.call(method, parameters, Gio.DBusCallFlags.NONE, timeoutMs, null,
                (source, result) => {
                    try {
                        source.call_finish(result);
                        resolve();
                    } catch (error) {
                        reject(error);
                    }
                });
        });
    }

    setMode(mode) {
        return this.call('SetMode', new GLib.Variant('(s)', [mode]));
    }

    setTentPrompt(enabled) {
        return this.call('SetTentPrompt', new GLib.Variant('(b)', [enabled]));
    }

    confirmTent(epoch) {
        return this.call('ConfirmTent', new GLib.Variant('(u)', [epoch]));
    }

    //: Replay the failed transaction.  ``Retry()`` is the 0.3.0 method; an
    //: older service without it falls back to re-selecting the current mode.
    async retry(fallbackMode = null) {
        try {
            await this.call('Retry');
        } catch (error) {
            if (!isUnknownMethodError(error) || !fallbackMode)
                throw error;
            await this.setMode(fallbackMode);
        }
    }

    // -- internals --------------------------------------------------------

    _emitState(text) {
        let state;
        try {
            state = parseState(text);
        } catch (error) {
            // A malformed state document is a service bug, not a transport
            // failure: keep the proxy and wait for the next state signal.
            logError(error);
            return;
        }
        if (!this._onState)
            return;
        try {
            this._onState(state);
        } catch (error) {
            // A page rendering error must not be reported as a bus failure.
            logError(error);
        }
    }

    _notifyUnavailable() {
        if (!this._onUnavailable)
            return;
        try {
            this._onUnavailable();
        } catch (error) {
            logError(error);
        }
    }

    _notifyConnected() {
        if (!this._onConnected)
            return;
        try {
            this._onConnected();
        } catch (error) {
            logError(error);
        }
    }

    _handleFailure(error) {
        logError(error);
        this._notifyUnavailable();
        this._dropProxy();
        this._scheduleReconnect();
    }

    _connect() {
        if (this._closed || this._creating || this._proxy)
            return;
        this._creating = true;
        Gio.DBusProxy.new(
            this._bus,
            Gio.DBusProxyFlags.NONE,
            null,
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            null,
            (source, result) => {
                this._creating = false;
                if (this._closed)
                    return;
                try {
                    this._proxy = Gio.DBusProxy.new_finish(result);
                } catch (error) {
                    logError(error);
                    this._notifyUnavailable();
                    this._scheduleReconnect();
                    return;
                }
                this._proxy.connect('g-signal',
                    (_proxy, _sender, signal, parameters) => this._onSignal(signal, parameters));
                this._proxy.connect('notify::g-name-owner', () => this._onOwnerChanged());
                this._onOwnerChanged();
            });
    }

    _onOwnerChanged() {
        if (!this._proxy)
            return;
        if (this._proxy.g_name_owner) {
            this._notifyConnected();
            this.refresh();
            return;
        }
        this._notifyUnavailable();
        this._dropProxy();
        this._scheduleReconnect();
    }

    _onSignal(signal, parameters) {
        if (signal !== 'StateChanged' || this._closed)
            return;
        try {
            this._emitState(parameters.deepUnpack()[0]);
        } catch (error) {
            // A malformed signal must not break the UI; the next refresh/state
            // signal will resynchronise.
            logError(error);
        }
    }

    _dropProxy() {
        // Dropping the only reference also disconnects the signal handlers.
        this._proxy = null;
    }

    _scheduleReconnect() {
        if (this._closed || this._retryId)
            return;
        this._retryId = GLib.timeout_add(
            GLib.PRIORITY_DEFAULT,
            RECONNECT_SECONDS * 1000,
            () => {
                this._retryId = 0;
                this._connect();
                return GLib.SOURCE_REMOVE;
            });
    }
}
