"""User-facing entry points.

Installed as ``/usr/bin/panasonic-posture-manager`` (GJS/libadwaita UI) and
``/usr/bin/panasonic-posture-manager-probe`` (read-only hardware probe via the
``panasonic_posture.hardware.probe`` entry point).
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .protocol import DATA_DIR

_UI_DIR_ENV = "PANASONIC_POSTURE_UI_DIR"


def _repository_root() -> Path:
    # src/panasonic_posture/cli.py -> repository root
    return Path(__file__).resolve().parents[2]


def locate_ui_dir() -> Path:
    """Return the directory containing the GJS UI entry point.

    Installed layout::

        /usr/share/panasonic-posture-manager/ui/main.js

    Source checkout layout::

        <repo>/data/ui/main.js
    """

    override = os.environ.get(_UI_DIR_ENV)
    if override:
        candidate = Path(override)
        if (candidate / "main.js").is_file():
            return candidate
        raise SystemExit(f"{_UI_DIR_ENV} does not contain main.js: {candidate}")
    installed = Path(DATA_DIR) / "ui"
    if (installed / "main.js").is_file():
        return installed
    source = _repository_root() / "data" / "ui"
    if (source / "main.js").is_file():
        return source
    raise SystemExit("cannot locate the panasonic-posture-manager UI files")


def launch_ui() -> None:
    gjs = shutil.which("gjs") or "/usr/bin/gjs"
    main_js = locate_ui_dir() / "main.js"
    argv = [gjs, "-m", str(main_js), *sys.argv[1:]]
    os.execv(gjs, argv)


if __name__ == "__main__":  # pragma: no cover - convenience for python -m
    launch_ui()
