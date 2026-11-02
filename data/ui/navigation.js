import Adw from 'gi://Adw?version=1';
import Gdk from 'gi://Gdk?version=4.0';
import Gtk from 'gi://Gtk?version=4.0';

export const NAVIGATION_CSS = `
viewswitcherbar.posture-navigation viewswitcher button.toggle:hover:not(:checked) {
    background-color: transparent;
    background-image: none;
    box-shadow: none;
}
`;

let styleInstalled = false;

function ensureNavigationStyle(display) {
    if (styleInstalled || !display)
        return;
    const provider = new Gtk.CssProvider();
    provider.load_from_data(NAVIGATION_CSS, -1);
    Gtk.StyleContext.add_provider_for_display(
        display,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION);
    styleInstalled = true;
}

export function createNavigationBar(stack) {
    ensureNavigationStyle(Gdk.Display.get_default());
    const bar = new Adw.ViewSwitcherBar({stack, reveal: true});
    bar.add_css_class('posture-navigation');
    return bar;
}
