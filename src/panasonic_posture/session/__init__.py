"""Shared session state machine plus the Linux desktop adapter.

``runtime``, ``ports``, ``failures``, ``transaction``, ``journal_schema`` and
``journal_store`` are platform-neutral and are reused by Windows.  The
remaining modules in this package are Linux D-Bus, desktop, display,
notification and default-path adapters; Windows session code lives under
``panasonic_posture.windows``.
"""
