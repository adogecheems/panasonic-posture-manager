// Application shell for the libadwaita window: builds the posture/settings
// pages and connects them to the D-Bus service.
//
// The application handles its command line explicitly (P3): a second launch
// such as `panasonic-posture-manager --settings` is forwarded by GApplication
// to the already running instance, which then switches to the settings page
// instead of silently staying on the posture page.  `createApplication` and
// `currentUi` are exported so the headless contract check can drive the same
// code path.
import Adw from 'gi://Adw?version=1';
import Gdk from 'gi://Gdk?version=4.0';
import GdkPixbuf from 'gi://GdkPixbuf?version=2.0';
import Gio from 'gi://Gio';
import Gtk from 'gi://Gtk?version=4.0';
import System from 'system';

import {DATA_ROOT, _} from './i18n.js';
import {createNavigationBar} from './navigation.js';
import {PosturePage} from './posturePage.js';
import {SettingsPage} from './settingsPage.js';
import {PostureService} from './service.js';

export const APPLICATION_ID = 'work.mmoe.panasonic.PostureManager';
const WINDOW_ICON_NAME = 'lets-note-window-icon';
const WINDOW_ICON_DARK_NAME = 'lets-note-window-icon-dark';
const WINDOW_MARK_WIDTH = 72;
const ASSETS_DIR = `${DATA_ROOT}/assets`;

let window = null;
let service = null;
let ui = null;

function configureApplicationIcon() {
    // The source checkout is not installed into the hicolor icon theme, so
    // expose its asset directory to GTK as well.  Installed builds resolve the
    // same application-id name from /usr/share/icons/hicolor.
    const display = Gdk.Display.get_default();
    if (display) {
        const theme = Gtk.IconTheme.get_for_display(display);
        if (!(theme.get_search_path() || []).includes(ASSETS_DIR))
            theme.add_search_path(ASSETS_DIR);
    }
    Gtk.Window.set_default_icon_name(APPLICATION_ID);
}

function windowIconName(styleManager) {
    return styleManager.dark ? WINDOW_ICON_DARK_NAME : WINDOW_ICON_NAME;
}

function loadWindowMark(name) {
    try {
        return GdkPixbuf.Pixbuf.new_from_file(`${ASSETS_DIR}/${name}.png`);
    } catch (error) {
        logError(error, `cannot load window mark ${name}`);
        return null;
    }
}

function createWindowMark() {
    const styleManager = Adw.StyleManager.get_default();
    let pixbuf = loadWindowMark(windowIconName(styleManager));
    if (!pixbuf)
        return null;

    const height = Math.round(WINDOW_MARK_WIDTH * pixbuf.get_height() / pixbuf.get_width());
    const mark = new Gtk.DrawingArea({
        content_width: WINDOW_MARK_WIDTH,
        content_height: height,
        halign: Gtk.Align.CENTER,
        valign: Gtk.Align.CENTER,
        margin_start: 12,
        accessible_role: Gtk.AccessibleRole.IMG,
    });
    mark.add_css_class('window-mark');
    mark.update_property([Gtk.AccessibleProperty.LABEL], ["Let's note"]);
    mark.set_draw_func((_area, cr, width, height) => {
        // Drawing the source at its intrinsic size keeps the wide mark crisp
        // on HiDPI displays while the widget itself stays at a fixed logical
        // width and never pushes the title bar taller than the logo.
        const scale = Math.min(width / pixbuf.get_width(), height / pixbuf.get_height());
        const drawWidth = pixbuf.get_width() * scale;
        const drawHeight = pixbuf.get_height() * scale;
        cr.save();
        cr.translate((width - drawWidth) / 2, (height - drawHeight) / 2);
        cr.scale(scale, scale);
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0);
        cr.paint();
        cr.restore();
    });

    // Keep the titlebar mark and the window's icon in sync with the desktop
    // colour scheme.  The taskbar icon stays tied to APPLICATION_ID through
    // the desktop entry and is intentionally not changed here.
    styleManager.connect('notify::dark', () => {
        const name = windowIconName(styleManager);
        const nextPixbuf = loadWindowMark(name);
        if (nextPixbuf) {
            pixbuf = nextPixbuf;
            mark.queue_draw();
        }
        if (window)
            window.set_icon_name(name);
    });
    return mark;
}

function removeWindowIconFromDecoration(header) {
    const layout = Gtk.Settings.get_default()?.gtk_decoration_layout;
    if (!layout)
        return;
    const withoutIcon = layout
        .split(':')
        .map(side => side.split(',').filter(button => button !== 'icon').join(','))
        .join(':');
    if (withoutIcon !== layout)
        header.decoration_layout = withoutIcon;
}

export function buildUi(app, postureService = null) {
    if (window) {
        window.present();
        return ui;
    }
    service = postureService || new PostureService();
    configureApplicationIcon();
    window = new Adw.ApplicationWindow({
        application: app,
        title: _('Panasonic 2-in-1 Tablet Posture Management Tool'),
        default_width: 500,
        default_height: 660,
    });
    window.set_icon_name(windowIconName(Adw.StyleManager.get_default()));

    const toolbar = new Adw.ToolbarView();
    window.content = toolbar;
    const header = new Adw.HeaderBar();
    removeWindowIconFromDecoration(header);
    const windowMark = createWindowMark();
    if (windowMark)
        header.pack_start(windowMark);
    header.title_widget = new Adw.WindowTitle({
        title: _('Panasonic 2-in-1 Tablet Posture Management Tool'),
    });
    toolbar.add_top_bar(header);

    const toastOverlay = new Adw.ToastOverlay();
    toolbar.content = toastOverlay;
    const stack = new Adw.ViewStack({vexpand: true});
    toastOverlay.child = stack;
    const toast = message => toastOverlay.add_toast(new Adw.Toast({title: message}));

    const posturePage = new PosturePage(service, toast);
    const settingsPage = new SettingsPage(service, toast);
    stack.add_titled_with_icon(posturePage.widget, 'posture', _('Current posture'), 'computer-symbolic');
    stack.add_titled_with_icon(settingsPage.widget, 'settings', _('Settings'), 'preferences-system-symbolic');
    toolbar.add_bottom_bar(createNavigationBar(stack));

    ui = {window, posturePage, settingsPage, service, stack};
    window.present();
    return ui;
}

function startService() {
    ui.service.start({
        onState: state => {
            ui.posturePage.update(state);
            ui.settingsPage.update(state);
        },
        onUnavailable: () => {
            ui.posturePage.setUnavailable();
            ui.settingsPage.setUnavailable();
        },
    });
}

function ensureUi(application) {
    if (!ui) {
        const created = buildUi(application);
        if (created)
            startService();
    } else {
        ui.window.present();
    }
    return ui;
}

//: Parse a GApplicationCommandLine (or a plain argv-like object in tests) into
//: the page that should be shown, or ``null`` for the default page.
//:
//: ``--settings`` is deliberately *not* registered as a GApplication main
//: option: remote command lines do not forward the parsed options dictionary,
//: but they do forward positional arguments, so the flag keeps working for a
//: second launch while the application is already running.
export function requestedPageFromCommandLine(commandLine) {
    try {
        const args = commandLine.get_arguments ? commandLine.get_arguments() : [];
        if (args.slice(1).includes('--settings'))
            return 'settings';
    } catch (error) {
        logError(error);
    }
    try {
        const options = commandLine.get_options_dict ? commandLine.get_options_dict() : null;
        if (options && options.lookup_value && options.lookup_value('settings', null))
            return 'settings';
    } catch (error) {
        logError(error);
    }
    return null;
}

export function applyRequestedPage(page) {
    if (ui && page)
        ui.stack.set_visible_child_name(page);
}

export function currentUi() {
    return ui;
}

export function createApplication(applicationId = APPLICATION_ID) {
    const application = new Adw.Application({
        application_id: applicationId,
        flags: Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
    });
    application.connect('command-line', (_application, commandLine) => {
        const target = ensureUi(application);
        if (target)
            applyRequestedPage(requestedPageFromCommandLine(commandLine));
        return 0;
    });
    application.connect('activate', () => ensureUi(application));
    application.connect('shutdown', () => {
        ui?.posturePage.close();
        if (service) {
            service.stop();
            service = null;
        }
        window = null;
        ui = null;
    });
    return application;
}

export function run(argv = []) {
    const application = createApplication();
    return application.run([System.programInvocationName, ...argv]);
}
