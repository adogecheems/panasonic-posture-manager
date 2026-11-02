// Theme-aware loading of the monochrome posture illustrations.
//
// The SVG sources use currentColor and var(--app-bg, white), which are useful
// in a browser but not resolved by librsvg inside Gtk.Picture.  Substitute the
// real Adwaita palette and rasterize the vector at 640x480; the UI draws it
// into a fixed-size Gtk.DrawingArea so the line art never gets upscaled.
//
// Robustness (P3): the pose name is validated against the asset whitelist, a
// failed pose falls back to the "unknown" illustration instead of showing a
// blank area, and the pixbuf cache is bounded (the old cache kept up to 20
// 640x480 RGBA buffers, about 25 MiB, forever).
//
// Running `gjs -m data/ui/illustration.js` renders every pose/theme variant
// once as a self-check; it prints nothing and exits 0 on success.
import GdkPixbuf from 'gi://GdkPixbuf?version=2.0';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import System from 'system';

import {DATA_ROOT} from './i18n.js';

export const POSES = ['laptop', 'tent', 'stand', 'tablet', 'unknown'];
export const POSE_SET = new Set(POSES);
export const ICON_WIDTH = 640;
export const ICON_HEIGHT = 480;
const CACHE_LIMIT = 8;

const cache = new Map();

function palette(dark, highContrast) {
    if (highContrast) {
        return dark ? {foreground: '#ffffff', background: '#000000'}
                    : {foreground: '#000000', background: '#ffffff'};
    }
    return dark ? {foreground: '#deddda', background: '#242424'}
                : {foreground: '#3d3846', background: '#fafafa'};
}

function normalizePose(pose) {
    return POSE_SET.has(pose) ? pose : 'unknown';
}

function cacheKey(root, pose, dark, highContrast) {
    return `${root}|${pose}|${dark ? 'dark' : 'light'}|${highContrast ? 'hc' : 'normal'}`;
}

function remember(key, pixbuf) {
    if (cache.size >= CACHE_LIMIT) {
        const oldest = cache.keys().next().value;
        if (oldest !== undefined)
            cache.delete(oldest);
    }
    cache.set(key, pixbuf);
    return pixbuf;
}

function rasterize(root, pose, colors) {
    const path = `${root}/assets/${pose}.svg`;
    const [ok, bytes] = GLib.file_get_contents(path);
    if (!ok)
        throw new Error(`cannot read ${path}`);
    const svg = new TextDecoder().decode(bytes)
        .replaceAll('currentColor', colors.foreground)
        .replaceAll('var(--app-bg, white)', colors.background);
    const stream = Gio.MemoryInputStream.new_from_bytes(
        new GLib.Bytes(new TextEncoder().encode(svg)));
    const pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(
        stream, ICON_WIDTH, ICON_HEIGHT, true, null);
    if (!pixbuf)
        throw new Error(`cannot decode ${path}`);
    return pixbuf;
}

export function posturePixbuf(root, pose, dark, highContrast = false) {
    const normalized = normalizePose(pose);
    const key = cacheKey(root, normalized, dark, highContrast);
    const cached = cache.get(key);
    if (cached) {
        // Touch for a simple LRU eviction order.
        cache.delete(key);
        cache.set(key, cached);
        return cached;
    }
    const colors = palette(dark, highContrast);
    let pixbuf;
    try {
        pixbuf = rasterize(root, normalized, colors);
    } catch (error) {
        logError(error);
        if (normalized === 'unknown')
            throw error;
        const fallbackKey = cacheKey(root, 'unknown', dark, highContrast);
        const fallback = cache.get(fallbackKey);
        pixbuf = fallback || rasterize(root, 'unknown', colors);
        remember(fallbackKey, pixbuf);
    }
    return remember(key, pixbuf);
}

export function clearCache() {
    cache.clear();
}

export function cacheSize() {
    return cache.size;
}

// -- self-check when executed directly ------------------------------------

function runningAsMain() {
    try {
        const self = GLib.filename_from_uri(import.meta.url)[0];
        const invoked = GLib.canonicalize_filename(
            System.programInvocationName, GLib.get_current_dir());
        return invoked === GLib.canonicalize_filename(self, GLib.get_current_dir());
    } catch (error) {
        return false;
    }
}

if (runningAsMain()) {
    let failures = 0;
    for (const pose of POSES) {
        for (const dark of [false, true]) {
            for (const highContrast of [false, true]) {
                try {
                    const pixbuf = posturePixbuf(DATA_ROOT, pose, dark, highContrast);
                    if (pixbuf.get_width() !== ICON_WIDTH || pixbuf.get_height() !== ICON_HEIGHT)
                        throw new Error('unexpected pixbuf size');
                } catch (error) {
                    failures += 1;
                    printerr(`illustration self-check failed (${pose}): ${error.message}`);
                }
            }
        }
    }
    if (cacheSize() > CACHE_LIMIT) {
        failures += 1;
        printerr(`illustration cache exceeded its limit (${cacheSize()})`);
    }
    System.exit(failures === 0 ? 0 : 1);
}
