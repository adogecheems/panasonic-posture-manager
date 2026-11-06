"""Single-instance libadwaita application and window shell."""
from typing import Callable
import sys
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, Gtk
from .common import DATA_ROOT, _
from .illustration import draw_pixbuf
from .posture import PosturePage
from .settings import SettingsPage
from .service import PostureService

APPLICATION_ID = 'work.mmoe.panasonic.PostureManager'
NAVIGATION_CSS = b'''
viewswitcherbar.posture-navigation viewswitcher button.toggle:hover:not(:checked) {
    background-color: alpha(currentColor, 0.08);
    background-image: none;
    box-shadow: none;
}
'''


class Application(Adw.Application):
    def __init__(self, application_id: str = APPLICATION_ID,
                 service_factory: Callable[[], PostureService] = PostureService) -> None:
        super().__init__(application_id=application_id, flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self._service_factory = service_factory
        self.window = None
        self.service = None
        self._style_handler = None
        self.connect('activate', lambda app: self.ensure_ui())
        self.connect('command-line', self._command_line)
        self.connect('shutdown', self._shutdown)

    def _command_line(self, app, command_line):
        self.ensure_ui()
        if '--settings' in command_line.get_arguments()[1:]:
            self.stack.set_visible_child_name('settings')
        return 0

    def ensure_ui(self) -> None:
        if self.window is not None:
            self.window.present()
            return
        self.service = self._service_factory()
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(DATA_ROOT / 'assets'))
        Gtk.Window.set_default_icon_name(APPLICATION_ID)
        self.window = Adw.ApplicationWindow(application=self, title=_('Panasonic 2-in-1 Tablet Posture Management Tool'), default_width=500, default_height=660)
        toolbar = Adw.ToolbarView()
        self.window.set_content(toolbar)
        header = Adw.HeaderBar()
        layout = Gtk.Settings.get_default().get_property('gtk-decoration-layout')
        if layout:
            header.set_decoration_layout(':'.join(','.join(b for b in side.split(',') if b != 'icon') for side in layout.split(':')))
        self.mark = Gtk.DrawingArea(content_width=72, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, margin_start=12, accessible_role=Gtk.AccessibleRole.IMG)
        self.mark.update_property([Gtk.AccessibleProperty.LABEL], ["Let's note"])
        self._mark_pixbuf = None
        self.mark.set_draw_func(lambda area, cr, w, h: draw_pixbuf(cr, self._mark_pixbuf, w, h))
        header.pack_start(self.mark)
        self._style = Adw.StyleManager.get_default()
        self._style_handler = self._style.connect('notify::dark', self._update_mark)
        self._update_mark()
        header.set_title_widget(Adw.WindowTitle(title=_('Panasonic 2-in-1 Tablet Posture Management Tool')))
        toolbar.add_top_bar(header)
        overlay = Adw.ToastOverlay()
        toolbar.set_content(overlay)
        self.stack = Adw.ViewStack(vexpand=True)
        overlay.set_child(self.stack)
        toast = lambda text: overlay.add_toast(Adw.Toast(title=text))
        self.posture_page = PosturePage(self.service, toast)
        self.settings_page = SettingsPage(self.service, toast)
        self.stack.add_titled_with_icon(self.posture_page.widget, 'posture', _('Current posture'), 'computer-symbolic')
        self.stack.add_titled_with_icon(self.settings_page.widget, 'settings', _('Settings'), 'preferences-system-symbolic')
        provider = Gtk.CssProvider()
        provider.load_from_data(NAVIGATION_CSS)
        Gtk.StyleContext.add_provider_for_display(display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        bar = Adw.ViewSwitcherBar(stack=self.stack, reveal=True)
        bar.add_css_class('posture-navigation')
        toolbar.add_bottom_bar(bar)
        self.service.start(self._state, self._unavailable)
        self.window.present()

    def _update_mark(self, *args):
        name = 'lets-note-window-icon-dark' if self._style.get_dark() else 'lets-note-window-icon'
        self.window.set_icon_name(name)
        path = DATA_ROOT / 'assets' / f'{name}.png'
        try:
            self._mark_pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
        except Exception:
            import logging
            logging.getLogger(__name__).exception('Cannot load window mark')
            self.mark.set_visible(False)
            return
        self.mark.set_content_height(round(72 * self._mark_pixbuf.get_height() / self._mark_pixbuf.get_width()))
        self.mark.set_visible(True)
        self.mark.queue_draw()

    def _state(self, state):
        self.posture_page.update(state)
        self.settings_page.update(state)

    def _unavailable(self):
        self.posture_page.set_unavailable()
        self.settings_page.set_unavailable()

    def _shutdown(self, app):
        if self.service:
            self.posture_page.close()
            self.settings_page.set_unavailable()
            self.service.stop()
            self.service = None
        if self._style_handler:
            self._style.disconnect(self._style_handler)
            self._style_handler = None
        self.window = None


def run(argv: list[str] | None = None) -> int:
    return Application().run(sys.argv if argv is None else argv)
