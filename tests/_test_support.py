"""Cross-platform helpers for platform-specific test modules."""

from __future__ import annotations

import sys
import unittest


def require_linux() -> None:
    """Skip a module before it imports Linux-only dependencies."""
    if sys.platform != "linux":
        raise unittest.SkipTest("requires the Linux platform adapters")
