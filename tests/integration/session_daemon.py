#!/usr/bin/python
"""Launch the system daemon on the *session* bus for integration tests only.

This helper is spawned by ``tests/integration/check_dbus.py`` inside
``dbus-run-session``.  It starts the real daemon implementation (not a fake)
with a private settings file and without touching the system bus, so the
D-Bus contract and state serialization can be tested without root.

Usage::

    session_daemon.py <config-path>
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from panasonic_posture.service.daemon import main  # noqa: E402  (after sys.path setup)


def run(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit("usage: session_daemon.py <config-path>")
    return main(["--session-bus", "--config", args[0]])


if __name__ == "__main__":
    raise SystemExit(run())
