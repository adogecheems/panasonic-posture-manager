// Settings page: automatic-mode toggles, posture prompts and manual mode.
//
// All writes are asynchronous.  The selected value is reflected immediately,
// while only one request per control is in flight; rapid changes collapse to
// the latest choice.  A failed write resynchronises the controls with the last
// known state instead of leaving them out of sync with the service.
import Adw from 'gi://Adw?version=1';
import Gdk from 'gi://Gdk?version=4.0';
import Gtk from 'gi://Gtk?version=4.0';
import GLib from 'gi://GLib';

import {_} from './i18n.js';
import {describeError} from './errors.js';

export const AUTHOR_NAME = 'adogecheems';
export const APP_VERSION = '2.3.2';
export const GITHUB_URL = 'https://github.com/adogecheems/panasonic-posture-manager';

const MODES = [
    ['auto', _('Automatic'), _('Switch automatically to suit the current posture')],
    ['laptop', _('Laptop'), _('Best for typing and precise work')],
    ['tent', _('Tent'), _('Turns the screen upside down and disables the built-in keyboard and touchpad')],
    ['stand', _('Stand'), _('Keeps the screen upright and disables the built-in keyboard and touchpad')],
    ['tablet', _('Tablet'), _('Lets the screen rotate and disables the built-in keyboard and touchpad')],
];

const PREVIOUS_MODE_KEYS = new Set([Gdk.KEY_Up, Gdk.KEY_Left, Gdk.KEY_KP_Up, Gdk.KEY_KP_Left]);
const NEXT_MODE_KEYS = new Set([Gdk.KEY_Down, Gdk.KEY_Right, Gdk.KEY_KP_Down, Gdk.KEY_KP_Right]);

export class SettingsPage {
    constructor(service, toast = () => {}) {
        this._service = service;
        this._toast = toast;
        this._syncing = false;
        this._inFlight = false;
        this._pendingMode = null;
        this._modeWriteActive = false;
        this._pendingTentMode = null;
        this._tentModeWriteActive = false;
        this._pendingTentPrompt = null;
        this._promptWriteActive = false;
        this._pendingStandPrompt = null;
        this._standWriteActive = false;
        this._radios = new Map();
        this._latestState = null;
        this._calibrating = false;
        this._build();
        this.setUnavailable();
    }

    get widget() {
        return this._page;
    }

    // -- public handlers --------------------------------------------------

    update(state) {
        if (!state)
            return;
        this._syncing = true;
        try {
            this._latestState = state;
            const tentEnabled = this._pendingTentMode === null
                ? state.tent_enabled !== false
                : this._pendingTentMode;
            this._tentModeSwitch.active = tentEnabled;
            const prompt = this._pendingTentPrompt === null
                ? Boolean(state.tent_prompt)
                : this._pendingTentPrompt;
            this._tentPromptSwitch.active = prompt;
            const standPrompt = this._pendingStandPrompt === null
                ? Boolean(state.stand_prompt)
                : this._pendingStandPrompt;
            this._standSwitch.active = standPrompt;
            const mode = this._pendingMode?.mode ?? state.manual;
            const active = this._radios.get(mode);
            if (active)
                active.active = true;
            const selectable = state.hardware !== true && state.supported !== false;
            for (const [mode, button] of this._radios)
                button.sensitive = selectable && (mode !== 'tent' || tentEnabled);
            this._tentModeSwitch.sensitive = state.supported !== false;
            this._tentPromptSwitch.sensitive = state.supported !== false && tentEnabled;
            this._standSwitch.sensitive = state.supported !== false;
            this._applyCalibrationState();
        } finally {
            this._syncing = false;
        }
    }

    setUnavailable() {
        this._latestState = null;
        this._pendingMode = null;
        this._pendingTentMode = null;
        this._pendingTentPrompt = null;
        this._pendingStandPrompt = null;
        this._syncing = true;
        try {
            for (const button of this._radios.values())
                button.sensitive = false;
            this._tentModeSwitch.sensitive = false;
            this._tentPromptSwitch.sensitive = false;
            this._standSwitch.sensitive = false;
            this._calibrateButton.sensitive = false;
            this._resetButton.sensitive = false;
        } finally {
            this._syncing = false;
        }
    }

    // -- construction -----------------------------------------------------

    _build() {
        this._page = new Adw.PreferencesPage();

        const notificationGroup = new Adw.PreferencesGroup({title: _('Automatic mode')});
        this._page.add(notificationGroup);
        this._tentModeSwitch = new Adw.SwitchRow({
            title: _('Enable tent mode'),
            subtitle: _('Allow automatic detection of tent mode'),
        });
        this._tentModeSwitch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Enable tent mode')]);
        notificationGroup.add(this._tentModeSwitch);
        this._tentModeSwitch.connect('notify::active', () => {
            if (this._syncing || !this._tentModeSwitch.sensitive)
                return;
            this._queueTentMode(this._tentModeSwitch.active);
        });

        this._tentPromptSwitch = new Adw.SwitchRow({
            title: _('Tent mode notification'),
            subtitle: _('Ask before switching to tent mode when the base is upright'),
        });
        this._tentPromptSwitch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Tent mode notification')]);
        notificationGroup.add(this._tentPromptSwitch);
        this._tentPromptSwitch.connect('notify::active', () => {
            if (this._syncing || !this._tentPromptSwitch.sensitive)
                return;
            this._queueTentPrompt(this._tentPromptSwitch.active);
        });

        this._standSwitch = new Adw.SwitchRow({
            title: _('Stand mode notification'),
            subtitle: _('Ask before switching to stand mode when the base is folded back'),
        });
        this._standSwitch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Stand mode notification')]);
        notificationGroup.add(this._standSwitch);
        this._standSwitch.connect('notify::active', () => {
            if (this._syncing || !this._standSwitch.sensitive)
                return;
            this._queueStandPrompt(this._standSwitch.active);
        });

        const sensorGroup = new Adw.PreferencesGroup({title: _('Sensor')});
        this._page.add(sensorGroup);
        this._calibrationRow = new Adw.ActionRow({
            title: _('Sensor calibration'),
            subtitle: _('Place the device in laptop form on a flat desk, then calibrate.'),
        });
        this._calibrationRow.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Sensor calibration')]);
        sensorGroup.add(this._calibrationRow);

        this._resetButton = new Gtk.Button({
            label: _('Reset'),
            valign: Gtk.Align.CENTER,
            visible: false,
        });
        this._resetButton.add_css_class('flat');
        this._resetButton.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Reset sensor calibration')]);
        this._resetButton.connect('clicked', () => {
            this._resetSensorCalibration().catch(logError);
        });

        this._calibrateButton = new Gtk.Button({
            label: _('Calibrate'),
            valign: Gtk.Align.CENTER,
            sensitive: false,
        });
        this._calibrateButton.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Calibrate sensor')]);
        this._calibrateButton.connect('clicked', () => this._showCalibrationDialog());

        const calibrationActions = new Gtk.Box({
            orientation: Gtk.Orientation.HORIZONTAL,
            spacing: 6,
            valign: Gtk.Align.CENTER,
        });
        calibrationActions.append(this._resetButton);
        calibrationActions.append(this._calibrateButton);
        this._calibrationRow.add_suffix(calibrationActions);

        const modeGroup = new Adw.PreferencesGroup({title: _('Mode selection')});
        this._page.add(modeGroup);
        let first = null;
        for (const [key, title, subtitle] of MODES) {
            const button = new Gtk.CheckButton({valign: Gtk.Align.CENTER});
            if (first)
                button.group = first;
            else
                first = button;
            button.update_property([Gtk.AccessibleProperty.LABEL], [title]);
            const row = new Adw.ActionRow({title, subtitle, activatable_widget: button});
            row.add_prefix(button);
            modeGroup.add(row);
            this._radios.set(key, button);
            // GTK radio navigation selects on arrow keys by default. Keep
            // browsing separate from an action that can disable the keyboard.
            const keys = new Gtk.EventControllerKey({
                propagation_phase: Gtk.PropagationPhase.CAPTURE,
            });
            keys.connect('key-pressed', (_controller, keyval) => this._browseModes(button, keyval));
            button.add_controller(keys);
            button.connect('toggled', () => {
                if (this._syncing || !button.active || !button.sensitive)
                    return;
                this._queueMode(key);
            });
        }

        const aboutGroup = new Adw.PreferencesGroup({title: _('About')});
        this._page.add(aboutGroup);
        aboutGroup.add(new Adw.ActionRow({
            title: _('Version'),
            subtitle: APP_VERSION,
        }));
        aboutGroup.add(new Adw.ActionRow({
            title: _('Author'),
            subtitle: AUTHOR_NAME,
        }));

        const githubButton = new Gtk.LinkButton({
            label: _('Visit repository'),
            uri: GITHUB_URL,
            valign: Gtk.Align.CENTER,
        });
        const githubRow = new Adw.ActionRow({
            title: _('GitHub'),
            subtitle: 'github.com/adogecheems/panasonic-posture-manager',
            activatable_widget: githubButton,
        });
        githubRow.add_suffix(githubButton);
        aboutGroup.add(githubRow);

        const trademarkNotice = new Gtk.Label({
            label: _("Let's note is a trademark of Panasonic Corporation. The author does not claim any rights to the trademark and is not associated with it."),
            wrap: true,
            xalign: 0,
            margin_start: 12,
            margin_end: 12,
            margin_top: 8,
            margin_bottom: 12,
        });
        trademarkNotice.add_css_class('dim-label');
        trademarkNotice.add_css_class('caption');
        aboutGroup.add(trademarkNotice);
    }

    // -- actions ----------------------------------------------------------

    _browseModes(button, keyval) {
        const step = PREVIOUS_MODE_KEYS.has(keyval) ? -1 : NEXT_MODE_KEYS.has(keyval) ? 1 : 0;
        if (!step)
            return false;
        const buttons = [...this._radios.values()].filter(choice => choice.sensitive);
        const index = buttons.indexOf(button);
        if (index !== -1)
            buttons[(index + step + buttons.length) % buttons.length].grab_focus();
        return true;
    }

    _applyCalibrationState() {
        if (!this._latestState)
            return;
        const calibrated = Boolean(this._latestState.sensor_calibrated);
        this._resetButton.visible = calibrated;
        this._calibrationRow.subtitle = calibrated
            ? _('A custom flat-desk reference is in use.')
            : _('Place the device in laptop form on a flat desk, then calibrate.');
        const enabled = !this._calibrating && this._latestState.supported !== false;
        this._calibrateButton.sensitive = enabled && this._latestState.hardware !== true;
        this._resetButton.sensitive = enabled;
    }

    _setCalibrationBusy(busy, action = null) {
        this._calibrating = busy;
        this._calibrateButton.label = busy && action === 'calibrate'
            ? _('Calibrating…')
            : _('Calibrate');
        this._resetButton.label = busy && action === 'reset'
            ? _('Resetting…')
            : _('Reset');
        if (busy) {
            this._calibrateButton.sensitive = false;
            this._resetButton.sensitive = false;
        } else {
            this._applyCalibrationState();
        }
    }

    _showCalibrationDialog() {
        if (this._calibrating)
            return;
        const dialog = new Adw.MessageDialog({
            transient_for: this._page.get_root(),
            modal: true,
            heading: _('Calibrate sensor?'),
            body: _('Place the device in laptop form on a flat, stable desk and wait about two seconds before confirming. Its current direction will become the level reference.'),
        });
        dialog.add_response('cancel', _('Cancel'));
        dialog.add_response('calibrate', _('Calibrate'));
        dialog.set_response_appearance('calibrate', Adw.ResponseAppearance.SUGGESTED);
        dialog.set_default_response('calibrate');
        dialog.set_close_response('cancel');
        dialog.connect('response', (_dialog, response) => {
            if (response === 'calibrate')
                this._calibrateSensor().catch(logError);
        });
        dialog.present();
    }

    async _calibrateSensor() {
        if (this._calibrating || !this._latestState)
            return;
        this._setCalibrationBusy(true, 'calibrate');
        try {
            await this._service.call('CalibrateSensor');
            this._toast(_('Sensor direction calibrated.'));
        } catch (error) {
            this._toast(`${_('Unable to calibrate sensor')}: ${describeError(error)}`);
        } finally {
            this._setCalibrationBusy(false);
            this._service.refresh();
        }
    }

    async _resetSensorCalibration() {
        if (this._calibrating || !this._latestState)
            return;
        this._setCalibrationBusy(true, 'reset');
        try {
            await this._service.call('ResetSensorCalibration');
            this._toast(_('Sensor direction reset.'));
        } catch (error) {
            this._toast(`${_('Unable to reset sensor calibration')}: ${describeError(error)}`);
        } finally {
            this._setCalibrationBusy(false);
            this._service.refresh();
        }
    }

    _queueMode(mode) {
        this._pendingMode = {mode};
        if (this._modeWriteActive)
            return;
        this._modeWriteActive = true;
        this._drainModeWrites().catch(logError);
    }

    async _drainModeWrites() {
        try {
            while (this._pendingMode !== null) {
                const requested = this._pendingMode;
                try {
                    await this._service.call(
                        'SetMode', new GLib.Variant('(s)', [requested.mode]));
                } catch (error) {
                    if (this._pendingMode !== null && this._pendingMode !== requested)
                        continue;
                    this._pendingMode = null;
                    this._toast(`${_('Unable to change settings')}: ${describeError(error)}`);
                    this._resync();
                    return;
                }
                if (this._pendingMode?.mode === requested.mode)
                    this._pendingMode = null;
            }
        } finally {
            this._modeWriteActive = false;
            this._service.refresh();
        }
    }

    _queueTentPrompt(enabled) {
        this._pendingTentPrompt = Boolean(enabled);
        if (this._promptWriteActive)
            return;
        this._promptWriteActive = true;
        this._drainTentPromptWrites().catch(logError);
    }

    _queueTentMode(enabled) {
        this._pendingTentMode = Boolean(enabled);
        if (this._tentModeWriteActive)
            return;
        this._tentModeWriteActive = true;
        this._drainTentModeWrites().catch(logError);
    }

    async _drainTentModeWrites() {
        try {
            while (this._pendingTentMode !== null) {
                const requested = this._pendingTentMode;
                try {
                    await this._service.call(
                        'SetTentEnabled', new GLib.Variant('(b)', [requested]));
                } catch (error) {
                    this._pendingTentMode = null;
                    this._toast(`${_('Unable to change settings')}: ${describeError(error)}`);
                    this._resync();
                    return;
                }
                if (this._pendingTentMode === requested)
                    this._pendingTentMode = null;
            }
        } finally {
            this._tentModeWriteActive = false;
            this._service.refresh();
        }
    }

    async _drainTentPromptWrites() {
        try {
            while (this._pendingTentPrompt !== null) {
                const requested = this._pendingTentPrompt;
                try {
                    await this._service.call(
                        'SetTentPrompt', new GLib.Variant('(b)', [requested]));
                } catch (error) {
                    this._pendingTentPrompt = null;
                    this._toast(`${_('Unable to change settings')}: ${describeError(error)}`);
                    this._resync();
                    return;
                }
                if (this._pendingTentPrompt === requested)
                    this._pendingTentPrompt = null;
            }
        } finally {
            this._promptWriteActive = false;
            this._service.refresh();
        }
    }

    _queueStandPrompt(enabled) {
        this._pendingStandPrompt = Boolean(enabled);
        if (this._standWriteActive)
            return;
        this._standWriteActive = true;
        this._drainStandPromptWrites().catch(logError);
    }

    async _drainStandPromptWrites() {
        try {
            while (this._pendingStandPrompt !== null) {
                const requested = this._pendingStandPrompt;
                try {
                    await this._service.call(
                        'SetStandPrompt', new GLib.Variant('(b)', [requested]));
                } catch (error) {
                    this._pendingStandPrompt = null;
                    this._toast(`${_('Unable to change settings')}: ${describeError(error)}`);
                    this._resync();
                    return;
                }
                if (this._pendingStandPrompt === requested)
                    this._pendingStandPrompt = null;
            }
        } finally {
            this._standWriteActive = false;
            this._service.refresh();
        }
    }

    _resync() {
        if (this._latestState)
            this.update(this._latestState);
    }

}
