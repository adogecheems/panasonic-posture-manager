#!/usr/bin/python
"""Safety guard for the manual integration tests.

If the parent process closes the pipe (or dies) or 25 s pass without the
single-byte confirmation ``c``, manual mode is reset to ``auto`` so a crashed
test cannot leave the machine in a software posture mode.
"""

import select
import sys
from pathlib import Path

from gi.repository import Gio, GLib

REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = REPO_ROOT / "src"
if _SRC_DIR.is_dir() and str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from panasonic_posture.protocol import IFACE, NAME, PATH  # noqa: E402

ready, _, _ = select.select([sys.stdin], [], [], 25)
if ready and sys.stdin.read(1) == "c":
    raise SystemExit(0)

bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
try:
    bus.call_sync(NAME, PATH, IFACE, "SetMode", GLib.Variant("(s)", ("auto",)),
                  None, Gio.DBusCallFlags.NO_AUTO_START, 4000, None)
except GLib.Error as error:
    print(f"reset guard: cannot restore automatic mode: {error.message}",
          file=sys.stderr)
