// Locale catalog and data directory discovery for the GJS UI.
//
// Installed layout:  /usr/share/panasonic-posture-manager/{ui,assets}
// Source checkout:   <repo>/data/{ui,assets}
// Locale catalog:    /usr/share/locale, else <repo>/locale (built by `make`)
//
// No language is hard-coded: the catalog is looked up for every language name
// that GLib reports.  Importing this module initialises gettext once; all
// other modules use the exported `_()` function so the literals stay
// discoverable by tests/check_i18n.py.
import Gettext from 'gettext';
import GLib from 'gi://GLib';

export const DOMAIN = 'panasonic-posture-manager';
export const INSTALLED_LOCALE_ROOT = '/usr/share/locale';

const thisFile = GLib.filename_from_uri(import.meta.url)[0];
export const UI_DIR = GLib.path_get_dirname(thisFile);
// data/ui -> data (or /usr/share/panasonic-posture-manager); assets live here.
export const DATA_ROOT = GLib.path_get_dirname(UI_DIR);
// data -> repository root (or /usr/share on installed systems).
export const REPOSITORY_ROOT = GLib.path_get_dirname(DATA_ROOT);

function isRegularFile(path) {
    return GLib.file_test(path, GLib.FileTest.IS_REGULAR);
}

function hasCatalog(root) {
    if (!root)
        return false;
    for (const language of GLib.get_language_names()) {
        if (isRegularFile(`${root}/${language}/LC_MESSAGES/${DOMAIN}.mo`))
            return true;
    }
    return false;
}

export function locateLocaleRoot() {
    const override = GLib.getenv('PANASONIC_POSTURE_LOCALE_DIR');
    if (override)
        return override;
    if (hasCatalog(INSTALLED_LOCALE_ROOT))
        return INSTALLED_LOCALE_ROOT;
    return `${REPOSITORY_ROOT}/locale`;
}

export const LOCALE_ROOT = locateLocaleRoot();
Gettext.bindtextdomain(DOMAIN, LOCALE_ROOT);
Gettext.textdomain(DOMAIN);

export function _(text) {
    return Gettext.gettext(text);
}
