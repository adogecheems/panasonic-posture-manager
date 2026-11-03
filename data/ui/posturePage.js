// "Current posture" page: illustration, state captions, posture confirmation
// and retry actions.  The page never blocks the UI: D-Bus calls are asynchronous,
// actions are de-duplicated while in flight and the settings-sync guard is
// released in a finally block (P3: an exception used to freeze `syncing`).
import Adw from 'gi://Adw?version=1';
import Gtk from 'gi://Gtk?version=4.0';
import Gdk from 'gi://Gdk?version=4.0';
import GLib from 'gi://GLib';

import {DATA_ROOT, _} from './i18n.js';
import {describeError, errorText} from './errors.js';
import {POSE_SET, posturePixbuf} from './illustration.js';

const POSE_NAMES = {
    laptop: _('Laptop'),
    tent: _('Tent'),
    stand: _('Stand'),
    tablet: _('Tablet'),
};

export class PosturePage {
    constructor(service, toast = () => {}) {
        this._service = service;
        this._toast = toast;
        this._syncing = false;
        this._inFlight = false;
        this._pose = 'laptop';
        this._pixbuf = null;
        this._pendingEpoch = null;
        this._pendingPrompt = null;
        this._latestState = null;
        this._build();
        this._updatePicture();
    }

    get widget() {
        return this._root;
    }

    // -- public handlers --------------------------------------------------

    update(state) {
        if (!state)
            return;
        this._syncing = true;
        try {
            this._latestState = state;
            // There is no user-facing "waiting for posture" state.  Keep the
            // last known drawing when an older daemon reports unknown.
            let pose = POSE_SET.has(state.pose) ? state.pose : 'unknown';
            if (pose === 'unknown')
                pose = this._pose;
            // The base can be nearly upright before tent mode was actually
            // applied: do not claim the tent illustration prematurely.
            if (pose === 'tent' && (!state.applied || state.applied.mode !== 'tent'))
                pose = 'laptop';
            if (pose !== this._pose) {
                this._pose = pose;
                this._updatePicture();
            }
            this._heading.label = POSE_NAMES[pose];
            this._drawing.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [POSE_NAMES[pose]]);
            const pendingEpoch = Number.isInteger(state.pending_notification)
                ? state.pending_notification
                : null;
            this._pendingPrompt = pendingEpoch === null
                ? null
                : state.pending_prompt === 'stand' ? 'stand' : 'tent';
            this._pendingEpoch = pendingEpoch;
            this._confirmButton.visible = this._pendingEpoch !== null;
            this._confirmButton.label = this._pendingPrompt === 'stand'
                ? _('Enter stand mode')
                : _('Enter tent mode');
            this._renderDetail(state);
            this._retryButton.visible =
                Boolean(state.error_code) && state.supported !== false;
            this._syncButtons();
        } finally {
            this._syncing = false;
        }
    }

    setUnavailable() {
        this._syncing = true;
        try {
            this._detail.visible = true;
            this._detail.label = _('Background service unavailable');
            this._confirmButton.visible = false;
            this._retryButton.visible = false;
        } finally {
            this._syncing = false;
        }
    }

    refreshTheme() {
        this._updatePicture();
    }

    // -- construction -----------------------------------------------------

    _build() {
        this._root = new Gtk.Box({
            orientation: Gtk.Orientation.VERTICAL,
            spacing: 14,
            valign: Gtk.Align.CENTER,
            margin_top: 24,
            margin_bottom: 24,
            margin_start: 24,
            margin_end: 24,
        });

        this._drawing = new Gtk.DrawingArea({
            width_request: 280,
            height_request: 190,
            halign: Gtk.Align.CENTER,
            valign: Gtk.Align.CENTER,
            accessible_role: Gtk.AccessibleRole.IMG,
        });
        this._drawing.set_draw_func((_area, cr, width, height) => this._draw(cr, width, height));
        this._drawing.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Current posture')]);
        this._root.append(this._drawing);

        this._heading = new Gtk.Label({
            label: POSE_NAMES.laptop,
            wrap: true,
            justify: Gtk.Justification.CENTER,
        });
        this._heading.add_css_class('title-1');
        this._root.append(this._heading);

        this._detail = new Gtk.Label({
            label: _('Connecting to background service…'),
            wrap: true,
            justify: Gtk.Justification.CENTER,
        });
        this._detail.add_css_class('dim-label');
        this._root.append(this._detail);

        this._confirmButton = new Gtk.Button({
            label: _('Enter tent mode'),
            visible: false,
            halign: Gtk.Align.CENTER,
        });
        this._confirmButton.add_css_class('suggested-action');
        this._confirmButton.connect('clicked', () => {
            const epoch = this._pendingEpoch;
            if (epoch === null)
                return;
            const method = this._pendingPrompt === 'stand'
                ? 'ConfirmStand'
                : 'ConfirmTent';
            this._request(method, new GLib.Variant('(u)', [epoch]), this._confirmButton);
        });
        this._root.append(this._confirmButton);

        this._retryButton = new Gtk.Button({
            label: _('Retry'),
            visible: false,
            halign: Gtk.Align.CENTER,
        });
        this._retryButton.connect('clicked', () => this._retry());
        this._root.append(this._retryButton);

        const style = Adw.StyleManager.get_default();
        style.connect('notify::dark', () => this._updatePicture());
        style.connect('notify::high-contrast', () => this._updatePicture());
    }

    // -- rendering --------------------------------------------------------

    //: The line under the posture heading is for the user, not a debug view.
    //: It only carries a translated error or a necessary readiness message.
    //: Hardware/sensor/profile details stay in GetState and the logs.
    _renderDetail(state) {
        let text = '';
        if (state.error_code) {
            text = `${_('Mode change failed')}: ${errorText(state.error_code)}`;
        } else if (!state.session_connected) {
            text = _('Waiting for the desktop to get ready…');
        } else if (state.supported === false) {
            text = _('This model does not support automatic mode changes.');
        } else if (state.support_tier === 'compatible') {
            text = _('Compatibility mode: this model is supported but has not been individually validated.');
        }
        this._detail.label = text;
        this._detail.visible = Boolean(text);
    }

    _updatePicture() {
        const style = Adw.StyleManager.get_default();
        try {
            this._pixbuf = posturePixbuf(
                DATA_ROOT, this._pose, style.get_dark(), style.get_high_contrast());
        } catch (error) {
            logError(error);
            this._pixbuf = null;
        }
        this._drawing.queue_draw();
    }

    _draw(cr, width, height) {
        const pixbuf = this._pixbuf;
        if (!pixbuf)
            return;
        const sourceWidth = pixbuf.get_width();
        const sourceHeight = pixbuf.get_height();
        const scale = Math.min(width / sourceWidth, height / sourceHeight);
        const drawWidth = sourceWidth * scale;
        const drawHeight = sourceHeight * scale;
        cr.save();
        cr.translate((width - drawWidth) / 2, (height - drawHeight) / 2);
        cr.scale(scale, scale);
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0);
        cr.paint();
        cr.restore();
    }

    // -- actions ----------------------------------------------------------

    async _request(method, parameters, button, busyLabel = null) {
        if (this._inFlight)
            return;
        await this._runAction(
            () => this._service.call(method, parameters),
            button,
            busyLabel || _('Applying mode…'));
    }

    //: The retry button replays the failed transaction through the dedicated
    //: ``Retry()`` method (F3/Q-02); old services fall back to SetMode.
    async _retry() {
        if (this._inFlight || !this._latestState)
            return;
        const fallbackMode = this._latestState.manual ?? null;
        await this._runAction(
            () => this._service.retry(fallbackMode),
            this._retryButton,
            _('Retrying…'));
    }

    async _runAction(action, button, busyLabel) {
        if (this._inFlight)
            return;
        this._inFlight = true;
        const previousLabel = button.label;
        button.label = busyLabel;
        this._syncButtons();
        try {
            await action();
            // Replay/refresh so the next state (or the cleared error) is
            // visible immediately even if the service does not emit a signal.
            this._service.refresh();
        } catch (error) {
            this._detail.visible = true;
            this._detail.label = describeError(error);
        } finally {
            this._inFlight = false;
            button.label = previousLabel;
            this._syncButtons();
        }
    }

    _syncButtons() {
        const busy = this._inFlight;
        this._confirmButton.sensitive = !busy;
        this._retryButton.sensitive = !busy;
    }
}
