"""Settings widgets with serial, coalesced asynchronous writes."""
from typing import Callable
from gi.repository import Adw, Gdk, Gtk
from .. import __version__
from ..frontend.ports import UIClient
from ..frontend.actions import SettingsModel, ActionRunner
from .common import _
from .errors import describe_error

AUTHOR_NAME = 'adogecheems'
APP_VERSION = __version__
GITHUB_URL = 'https://github.com/adogecheems/panasonic-posture-manager'
MODES = (
    ('auto', _('Automatic'), _('Switch automatically to suit the current posture')),
    ('laptop', _('Laptop'), _('Best for typing and precise work')),
    ('tent', _('Tent'), _('Turns the screen upside down and disables the built-in keyboard and touchpad')),
    ('stand', _('Stand'), _('Keeps the screen upright and disables the built-in keyboard and touchpad')),
    ('tablet', _('Tablet'), _('Lets the screen rotate and disables the built-in keyboard and touchpad')),
)
PREVIOUS_KEYS = {Gdk.KEY_Up, Gdk.KEY_Left, Gdk.KEY_KP_Up, Gdk.KEY_KP_Left}
NEXT_KEYS = {Gdk.KEY_Down, Gdk.KEY_Right, Gdk.KEY_KP_Down, Gdk.KEY_KP_Right}


class SettingsPage:
    def __init__(self, service: UIClient, toast: Callable[[str], None] = lambda message: None) -> None:
        self._service, self._toast = service, toast
        self._syncing = False
        self._latest_state = None
        self._model = SettingsModel(service, self._refresh_widgets, self._write_failed)
        self._calibration = ActionRunner(self._calibration_state, self._calibration_failed,
                                         self._calibration_succeeded)
        self.radios = {}
        self.widget = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_('Automatic mode'))
        self.widget.add(group)
        self.switches = {}
        for field, title, subtitle in (
            ('tent_enabled', _('Enable tent mode'), _('Allow automatic detection of tent mode')),
            ('tent_prompt', _('Tent mode notification'), _('Ask before switching to tent mode when the base is upright')),
            ('stand_prompt', _('Stand mode notification'), _('Ask before switching to stand mode when the base is folded back')),
        ):
            switch = Adw.SwitchRow(title=title, subtitle=subtitle)
            switch.update_property([Gtk.AccessibleProperty.LABEL], [title])
            group.add(switch)
            self.switches[field] = switch
            switch.connect('notify::active', lambda row, pspec, f=field: self._switch_changed(row, f))
        sensor = Adw.PreferencesGroup(title=_('Sensor'))
        self.widget.add(sensor)
        self.calibration_row = Adw.ActionRow(title=_('Sensor calibration'), subtitle=_('Place the device in laptop form on a flat desk, then calibrate.'))
        sensor.add(self.calibration_row)
        self.reset_button = Gtk.Button(label=_('Reset'), valign=Gtk.Align.CENTER, sensitive=False, visible=False)
        self.reset_button.add_css_class('flat')
        self.reset_button.update_property([Gtk.AccessibleProperty.LABEL], [_('Reset sensor calibration')])
        self.reset_button.connect('clicked', lambda button: self._calibrate(True))
        self.calibrate_button = Gtk.Button(label=_('Calibrate'), valign=Gtk.Align.CENTER, sensitive=False)
        self.calibrate_button.update_property([Gtk.AccessibleProperty.LABEL], [_('Calibrate sensor')])
        self.calibrate_button.connect('clicked', self._show_calibration_dialog)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, valign=Gtk.Align.CENTER)
        actions.append(self.reset_button)
        actions.append(self.calibrate_button)
        self.calibration_row.add_suffix(actions)
        modes = Adw.PreferencesGroup(title=_('Mode selection'))
        self.widget.add(modes)
        first = None
        for key, title, subtitle in MODES:
            button = Gtk.CheckButton(valign=Gtk.Align.CENTER)
            if first is None:
                first = button
            else:
                button.set_group(first)
            button.update_property([Gtk.AccessibleProperty.LABEL], [title])
            row = Adw.ActionRow(title=title, subtitle=subtitle, activatable_widget=button)
            row.add_prefix(button)
            modes.add(row)
            self.radios[key] = button
            keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
            keys.connect('key-pressed', lambda controller, keyval, keycode, state, b=button: self.browse_modes(b, keyval))
            button.add_controller(keys)
            button.connect('toggled', lambda b, k=key: self._mode_changed(b, k))
        about = Adw.PreferencesGroup(title=_('About'))
        self.widget.add(about)
        about.add(Adw.ActionRow(title=_('Version'), subtitle=APP_VERSION))
        about.add(Adw.ActionRow(title=_('Author'), subtitle=AUTHOR_NAME))
        link = Gtk.LinkButton(label=_('Visit repository'), uri=GITHUB_URL, valign=Gtk.Align.CENTER)
        row = Adw.ActionRow(title=_('GitHub'), subtitle='github.com/adogecheems/panasonic-posture-manager', activatable_widget=link)
        row.add_suffix(link)
        about.add(row)
        notice = Gtk.Label(label=_("Let's note is a trademark of Panasonic Corporation. The author does not claim any rights to the trademark and is not associated with it."), wrap=True, xalign=0, margin_start=12, margin_end=12, margin_top=8, margin_bottom=12)
        notice.add_css_class('dim-label')
        notice.add_css_class('caption')
        about.add(notice)
        self.set_unavailable()

    def update(self, state: dict) -> None:
        self._latest_state = state
        self._model.update(state)
        self._syncing = True
        try:
            for field, switch in self.switches.items():
                switch.set_active(self._model.value(field))
                switch.set_sensitive(self._model.preference_enabled(field))
            mode = self._model.value('manual')
            if mode in self.radios:
                self.radios[mode].set_active(True)
            for key, button in self.radios.items():
                button.set_sensitive(self._model.mode_enabled(key))
            self._calibration_state()
        finally:
            self._syncing = False

    def set_unavailable(self) -> None:
        self._model.disconnect()
        self._calibration.disconnect()
        self._latest_state = None
        for widget in (*self.radios.values(), *self.switches.values(), self.calibrate_button, self.reset_button):
            widget.set_sensitive(False)

    def browse_modes(self, button: Gtk.CheckButton, keyval: int) -> bool:
        step = -1 if keyval in PREVIOUS_KEYS else 1 if keyval in NEXT_KEYS else 0
        if not step:
            return False
        buttons = [b for b in self.radios.values() if b.get_sensitive()]
        if button in buttons:
            buttons[(buttons.index(button) + step) % len(buttons)].grab_focus()
        return True

    def _mode_changed(self, button, mode):
        if not self._syncing and button.get_active() and button.get_sensitive():
            self._model.queue('manual', mode)

    def _switch_changed(self, row, field):
        if not self._syncing and row.get_sensitive():
            self._model.queue(field, row.get_active())

    @property
    def _calibrating(self):
        return self._calibration.busy is not None

    def _refresh_widgets(self):
        if self._latest_state is not None:
            self.update(self._latest_state)

    def _write_failed(self, error):
        self._toast(f"{_('Unable to change settings')}: {describe_error(error)}")

    def _calibration_state(self):
        state = self._latest_state
        if state is None:
            return
        calibrated = state['sensor_calibrated']
        self.reset_button.set_visible(calibrated)
        self.calibration_row.set_subtitle(_('A custom flat-desk reference is in use.') if calibrated else _('Place the device in laptop form on a flat desk, then calibrate.'))
        enabled = not self._calibrating and state['supported']
        self.calibrate_button.set_sensitive(enabled and state['hardware'] is not True)
        self.reset_button.set_sensitive(enabled)
        if not self._calibrating:
            self.calibrate_button.set_label(_('Calibrate'))
            self.reset_button.set_label(_('Reset'))

    def _show_calibration_dialog(self, button):
        if self._calibrating or self._latest_state is None:
            return
        dialog = Adw.MessageDialog(transient_for=self.widget.get_root(), modal=True,
            heading=_('Calibrate sensor?'), body=_('Place the device in laptop form on a flat, stable desk and wait about two seconds before confirming. Its current direction will become the level reference.'))
        dialog.add_response('cancel', _('Cancel'))
        dialog.add_response('calibrate', _('Calibrate'))
        dialog.set_response_appearance('calibrate', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('calibrate')
        dialog.set_close_response('cancel')
        connection = self._model.connection
        dialog.connect('response', lambda d, response: self._calibrate(False) if response == 'calibrate' and connection == self._model.connection else None)
        dialog.present()

    def _calibrate(self, reset=False):
        if self._calibrating or self._latest_state is None:
            return
        self._calibration_reset = reset
        action = self._service.reset_calibration if reset else self._service.calibrate
        self._calibration.run('reset' if reset else 'calibrate', action)
        if self._calibrating:
            (self.reset_button if reset else self.calibrate_button).set_label(_('Resetting…') if reset else _('Calibrating…'))

    def _calibration_failed(self, error):
        prefix = _('Unable to reset sensor calibration') if self._calibration_reset else _('Unable to calibrate sensor')
        self._toast(f'{prefix}: {describe_error(error)}')
        self._service.refresh()

    def _calibration_succeeded(self):
        self._toast(_('Sensor direction reset.') if self._calibration_reset else _('Sensor direction calibrated.'))
        self._service.refresh()
