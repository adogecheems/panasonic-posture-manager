"""Current posture, delayed feedback and asynchronous confirmation actions."""
from typing import Callable
import logging
from gi.repository import Adw, Gtk, GLib
from .service import PostureService
from .common import DATA_ROOT, _
from .errors import describe_error
from ..session.errors import error_text
from .illustration import posture_pixbuf, draw_pixbuf

POSE_NAMES = {'laptop': _('Laptop'), 'tent': _('Tent'), 'stand': _('Stand'), 'tablet': _('Tablet')}


def displayed_pose(state: dict) -> str:
    applied = (state.get('applied') or {}).get('mode')
    if applied == 'hardware':
        return 'tablet'
    if applied in POSE_NAMES:
        return applied
    manual = state.get('manual')
    if not state.get('error_code') and manual in POSE_NAMES and (state.get('target') or {}).get('mode') == manual:
        return manual
    return state.get('pose') if state.get('pose') in POSE_NAMES else 'unknown'


class PosturePage:
    def __init__(self, service: PostureService, toast: Callable[[str], None] = lambda message: None) -> None:
        self._service = service
        self._latest_state = None
        self._pose = 'laptop'
        self._pixbuf = None
        self._busy_button = None
        self._epoch = None
        self._prompt = None
        self._transition = None
        self._timer = 0
        self._slow = False
        self._connection = 0
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                              valign=Gtk.Align.CENTER, margin_top=24, margin_bottom=24,
                              margin_start=24, margin_end=24)
        self.drawing = Gtk.DrawingArea(width_request=280, height_request=190,
                                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                                      accessible_role=Gtk.AccessibleRole.IMG)
        self.drawing.set_draw_func(lambda area, cr, w, h: draw_pixbuf(cr, self._pixbuf, w, h))
        self.drawing.update_property([Gtk.AccessibleProperty.LABEL], [_('Current posture')])
        self.heading = Gtk.Label(label=POSE_NAMES['laptop'], wrap=True, justify=Gtk.Justification.CENTER)
        self.heading.add_css_class('title-1')
        self.detail = Gtk.Label(label=_('Connecting to background service…'), wrap=True, justify=Gtk.Justification.CENTER)
        self.detail.add_css_class('dim-label')
        self.confirm_button = Gtk.Button(label=_('Enter tent mode'), visible=False, halign=Gtk.Align.CENTER)
        self.confirm_button.add_css_class('suggested-action')
        self.confirm_button.connect('clicked', self._confirm)
        self.retry_button = Gtk.Button(label=_('Retry'), visible=False, halign=Gtk.Align.CENTER)
        self.retry_button.connect('clicked', self._retry)
        for child in (self.drawing, self.heading, self.detail, self.confirm_button, self.retry_button):
            self.widget.append(child)
        self._style = Adw.StyleManager.get_default()
        self._style_handlers = [self._style.connect(signal, lambda *args: self.refresh_theme())
                                for signal in ('notify::dark', 'notify::high-contrast')]
        self.refresh_theme()

    def update(self, state: dict) -> None:
        self._latest_state = state
        mode = state['target']['mode']
        pending = not state['error_code'] and state['session_connected'] and state['supported'] and mode != 'unavailable' and (state['applied'] or {}).get('mode') != mode
        key = (state['instance'], state['generation'], mode)
        if not pending or self._transition != key:
            self._clear_transition()
            if pending:
                self._transition = key
                self._timer = GLib.timeout_add(2000, self._show_slow)
        pose = displayed_pose(state)
        if pose != 'unknown' and pose != self._pose:
            self._pose = pose
            self.refresh_theme()
        self.heading.set_label(POSE_NAMES[self._pose])
        self.drawing.update_property([Gtk.AccessibleProperty.LABEL], [POSE_NAMES[self._pose]])
        self._epoch = state['pending_notification']
        self._prompt = state['pending_prompt']
        self.confirm_button.set_visible(self._epoch is not None)
        self.retry_button.set_visible(bool(state['error_code']) and state['supported'])
        self._render_detail()
        self._sync_buttons()

    def _render_detail(self):
        state = self._latest_state
        if state is None:
            return
        text = ''
        if state['error_code']:
            text = f"{_('Mode change failed')}: {error_text(state['error_code'])}"
        elif not state['session_connected']:
            text = _('Waiting for the desktop to get ready…')
        elif not state['supported']:
            text = _('This model does not support automatic mode changes.')
        elif state['target']['mode'] == 'unavailable':
            if state['hardware'] is None:
                text = _('The tablet switch is unavailable. Restart the background service if this continues.')
            elif state['sensor_error'] or state['sensor_source'] == 'starting':
                text = _('No posture data is available. Keep the device still; switching will resume automatically.')
        elif self._slow:
            text = _('Switching is taking longer than usual. Please wait or choose another mode in Settings.')
        elif state['support_tier'] == 'compatible':
            text = _('Compatibility mode: this model is supported but has not been individually validated.')
        self.detail.set_label(text)
        self.detail.set_visible(bool(text))

    def _show_slow(self):
        self._timer = 0
        self._slow = True
        self._render_detail()
        return GLib.SOURCE_REMOVE

    def _clear_transition(self):
        self._transition = None
        self._slow = False
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def set_unavailable(self) -> None:
        self._connection += 1
        self._latest_state = None
        self._busy_button = None
        self._clear_transition()
        self.detail.set_label(_('Background service unavailable'))
        self.detail.set_visible(True)
        self.confirm_button.set_visible(False)
        self.retry_button.set_visible(False)

    def close(self) -> None:
        self.set_unavailable()
        for handler in self._style_handlers:
            self._style.disconnect(handler)
        self._style_handlers = []

    def refresh_theme(self) -> None:
        try:
            self._pixbuf = posture_pixbuf(DATA_ROOT, self._pose, self._style.get_dark(), self._style.get_high_contrast())
        except (OSError, GLib.Error):
            logging.getLogger(__name__).exception('Cannot load illustration')
            self._pixbuf = None
        self.drawing.queue_draw()

    def _confirm(self, button):
        if self._epoch is not None:
            self._run_action(lambda: self._service.call('ConfirmStand' if self._prompt == 'stand' else 'ConfirmTent', GLib.Variant('(u)', (self._epoch,))), button)

    def _retry(self, button):
        if self._latest_state:
            self._run_action(lambda: self._service.retry(self._latest_state['manual']), button)

    def _run_action(self, action, button):
        if self._busy_button:
            return
        self._busy_button = button
        connection = self._connection
        self._sync_buttons()

        def finished(result):
            if connection != self._connection:
                return
            try:
                result.result()
                self._service.refresh()
            except Exception as error:
                self.detail.set_label(describe_error(error))
                self.detail.set_visible(True)
            finally:
                self._busy_button = None
                self._sync_buttons()

        action().add_done_callback(finished)

    def _sync_buttons(self):
        self.confirm_button.set_label(_('Enter stand mode') if self._prompt == 'stand' else _('Enter tent mode'))
        self.retry_button.set_label(_('Retrying…') if self._busy_button is self.retry_button else _('Retry'))
        self.confirm_button.set_sensitive(self._busy_button is None)
        self.retry_button.set_sensitive(self._busy_button is None)
