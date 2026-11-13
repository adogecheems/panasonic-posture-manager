"""User-facing Python GTK/libadwaita entry point."""
from __future__ import annotations


def launch_ui() -> int:
    # Import GTK only for the GUI command; the probe remains display independent.
    from .ui.app import run
    return run()


if __name__ == '__main__':
    raise SystemExit(launch_ui())
