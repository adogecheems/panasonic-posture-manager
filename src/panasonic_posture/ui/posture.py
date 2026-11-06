"""Current posture, delayed feedback and asynchronous confirmation actions."""
from typing import Callable
import logging
import math
import time
from gi.repository import Adw, Gtk, GLib
from ..frontend.ports import UIClient
from ..frontend.state import PostureModel, Detail, displayed_pose
from ..frontend.actions import ActionRunner
from .common import DATA_ROOT, _
from .errors import describe_error
from ..session.errors import error_text
from .illustration import posture_pixbuf, draw_pixbuf

POSE_NAMES = {'laptop': _('Laptop'), 'tent': _('Tent'), 'stand': _('Stand'), 'tablet': _('Tablet')}


class PosturePage:
    def __init__(self, service: UIClient, toast: Callable[[str], None] = lambda message: None) -> None:
        self._service = service
        self._latest_state = None
        self._pose = 'laptop'
        self._pixbuf = None
        self._epoch = None
        self._prompt = None
        self._timer = 0
        self._model = PostureModel()
        self._actions = ActionRunner(self._sync_buttons, self._action_failed, service.refresh)
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
        deadline = self._model.deadline
        self._model.update(state, time.monotonic())
        if deadline != self._model.deadline:
            self._clear_timer()
            if self._model.deadline is not None:
                delay = max(1, math.ceil((self._model.deadline - time.monotonic()) * 1000))
                self._timer = GLib.timeout_add(delay, self._show_slow)
        pose = self._model.pose
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
        if self._model.detail == Detail.ERROR:
            text = f"{_('Mode change failed')}: {error_text(state['error_code'])}"
        elif self._model.detail == Detail.SESSION:
            text = _('Waiting for the desktop to get ready…')
        elif self._model.detail == Detail.UNSUPPORTED:
            text = _('This model does not support automatic mode changes.')
        elif self._model.detail == Detail.SWITCH:
            text = _('The tablet switch is unavailable. Restart the background service if this continues.')
        elif self._model.detail == Detail.SENSOR:
            text = _('No posture data is available. Keep the device still; switching will resume automatically.')
        elif self._model.detail == Detail.SLOW:
            text = _('Switching is taking longer than usual. Please wait or choose another mode in Settings.')
        elif self._model.detail == Detail.COMPATIBLE:
            text = _('Compatibility mode: this model is supported but has not been individually validated.')
        self.detail.set_label(text)
        self.detail.set_visible(bool(text))

    def _show_slow(self):
        self._timer = 0
        self._model.advance(time.monotonic())
        self._render_detail()
        return GLib.SOURCE_REMOVE

    @property
    def _slow(self):
        return self._model.slow

    def _clear_timer(self):
        if self._timer:
            GLib.source_remove(self._timer)
            self._timer = 0

    def set_unavailable(self) -> None:
        self._actions.disconnect()
        self._model.disconnect()
        self._latest_state = None
        self._clear_timer()
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
            self._actions.run('confirm', lambda: self._service.confirm_prompt(self._prompt, self._epoch))

    def _retry(self, button):
        if self._latest_state:
            self._actions.run('retry', lambda: self._service.retry(self._latest_state['manual']))

    def _action_failed(self, error):
        self.detail.set_label(describe_error(error))
        self.detail.set_visible(True)

    def _sync_buttons(self):
        self.confirm_button.set_label(_('Enter stand mode') if self._prompt == 'stand' else _('Enter tent mode'))
        self.retry_button.set_label(_('Retrying…') if self._actions.busy == 'retry' else _('Retry'))
        self.confirm_button.set_sensitive(self._actions.busy is None)
        self.retry_button.set_sensitive(self._actions.busy is None)
