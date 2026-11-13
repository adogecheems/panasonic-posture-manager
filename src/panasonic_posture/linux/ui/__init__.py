"""Unprivileged GTK 4/libadwaita interface, implemented with PyGObject."""
import gi

gi.require_version('Gtk', '4.0')
gi.require_version('Gdk', '4.0')
gi.require_version('Adw', '1')
gi.require_version('GdkPixbuf', '2.0')

# DrawingArea callbacks receive Cairo contexts via the GI foreign bridge.
gi.require_foreign('cairo')
