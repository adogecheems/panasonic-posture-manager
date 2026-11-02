// Settings page: tent-prompt toggle and manual mode selection.
//
// All writes are asynchronous and de-duplicated while in flight (P3: double
// clicks used to produce spurious "confirmation expired" errors).  A failed
// write resynchronises the controls with the last known state instead of
// leaving them out of sync with the service.
import Adw from 'gi://Adw?version=1';
import Gtk from 'gi://Gtk?version=4.0';
import GLib from 'gi://GLib';

import {_} from './i18n.js';
import {describeError} from './errors.js';

export const AUTHOR_NAME = 'adogecheems';
// The repository has not been published yet.  Keep the final URL here so the
// button starts working as soon as that repository is created.
export const GITHUB_URL = 'https://github.com/adogecheems/panasonic-posture-manager';

const MODES = [
    ['auto', _('Automatic'), _('Switch automatically to suit the current posture')],
    ['laptop', _('Laptop'), _('Best for typing and precise work')],
    ['tent', _('Tent'), _('Best for watching video and touch use')],
    ['stand', _('Stand'), _('Best for viewing and touch use')],
    ['tablet', _('Tablet'), _('Best for handheld or flat use')],
];

export class SettingsPage {
    constructor(service, toast = () => {}) {
        this._service = service;
        this._toast = toast;
        this._syncing = false;
        this._inFlight = false;
        this._radios = new Map();
        this._latestState = null;
        this._build();
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
            this._switch.active = Boolean(state.tent_prompt);
            const active = this._radios.get(state.manual);
            if (active)
                active.active = true;
            const selectable = !this._inFlight &&
                state.hardware !== true && state.supported !== false;
            for (const button of this._radios.values())
                button.sensitive = selectable;
            this._switch.sensitive = !this._inFlight && state.supported !== false;
        } finally {
            this._syncing = false;
        }
    }

    setUnavailable() {
        this._syncing = true;
        try {
            for (const button of this._radios.values())
                button.sensitive = false;
            this._switch.sensitive = false;
        } finally {
            this._syncing = false;
        }
    }

    // -- construction -----------------------------------------------------

    _build() {
        this._page = new Adw.PreferencesPage();

        const notificationGroup = new Adw.PreferencesGroup();
        this._page.add(notificationGroup);
        this._switch = new Adw.SwitchRow({
            title: _('Tent mode notification'),
            subtitle: _('Offer tent mode when the base is upright'),
        });
        this._switch.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_('Tent mode notification')]);
        notificationGroup.add(this._switch);
        this._switch.connect('notify::active', () => {
            if (this._syncing || !this._switch.sensitive)
                return;
            this._write(
                'SetTentPrompt',
                new GLib.Variant('(b)', [this._switch.active]));
        });

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
            button.connect('toggled', () => {
                if (this._syncing || !button.active || !button.sensitive)
                    return;
                this._write('SetMode', new GLib.Variant('(s)', [key]));
            });
        }

        const aboutGroup = new Adw.PreferencesGroup({title: _('About')});
        this._page.add(aboutGroup);
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
    }

    // -- actions ----------------------------------------------------------

    async _write(method, parameters) {
        if (this._inFlight) {
            this._resync();
            return;
        }
        this._inFlight = true;
        this._setControlsSensitive(false);
        try {
            await this._service.call(method, parameters);
        } catch (error) {
            this._toast(`${_('Unable to change settings')}: ${describeError(error)}`);
            this._resync();
        } finally {
            this._inFlight = false;
            this._setControlsSensitive(true);
            this._service.refresh();
        }
    }

    _resync() {
        if (this._latestState)
            this.update(this._latestState);
    }

    _setControlsSensitive(enabled) {
        const selectable = enabled && this._latestState &&
            this._latestState.hardware !== true && this._latestState.supported !== false;
        for (const button of this._radios.values())
            button.sensitive = Boolean(selectable);
        this._switch.sensitive = Boolean(enabled && this._latestState && this._latestState.supported !== false);
    }
}
