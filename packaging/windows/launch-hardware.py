"""Fixed source entry point for the current-user hardware UI at login."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from panasonic_posture.fluent.__main__ import main

raise SystemExit(main(['--background']))
