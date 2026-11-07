"""Theme-aware SVG rendering with bounded LRU storage."""
from collections import OrderedDict
from pathlib import Path
from gi.repository import Gdk, GdkPixbuf, Gio, GLib

from ..frontend.state import POSES

CACHE_LIMIT = 8
_CACHE = OrderedDict()


def posture_pixbuf(root: Path, pose: str, dark: bool = False,
                   high_contrast: bool = False) -> GdkPixbuf.Pixbuf | None:
    if pose not in POSES:
        return None
    key = (str(root), pose, dark, high_contrast)
    if key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    foreground, background = (('#ffffff', '#000000') if dark else ('#000000', '#ffffff')) if high_contrast else (('#deddda', '#242424') if dark else ('#3d3846', '#fafafa'))
    svg = (root / 'assets' / f'{pose}.svg').read_text(encoding='utf-8')
    svg = svg.replace('currentColor', foreground).replace('var(--app-bg, white)', background)
    stream = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg.encode()))
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream_at_scale(stream, 640, 480, True, None)
    finally:
        stream.close(None)
    _CACHE[key] = pixbuf
    while len(_CACHE) > CACHE_LIMIT:
        _CACHE.popitem(last=False)
    return pixbuf


def draw_pixbuf(cr, pixbuf: GdkPixbuf.Pixbuf | None, width: int, height: int) -> None:
    if pixbuf is None:
        return
    scale = min(width / pixbuf.get_width(), height / pixbuf.get_height())
    cr.save()
    cr.translate((width - pixbuf.get_width() * scale) / 2,
                 (height - pixbuf.get_height() * scale) / 2)
    cr.scale(scale, scale)
    Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
    cr.paint()
    cr.restore()
