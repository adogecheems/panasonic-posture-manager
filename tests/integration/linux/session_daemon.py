#!/usr/bin/python
"""Launch the system daemon on the *session* bus for integration tests only.

This helper is spawned by ``tests/integration/linux/check_dbus.py`` inside
``dbus-run-session``.  It starts the real daemon implementation (not a fake)
with a private settings file and without touching the system bus, so the
D-Bus contract and state serialization can be tested without root. The agent
check selects healthy hardware fixtures through this test-only helper.

Usage::

    session_daemon.py <config-path>
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.is_dir() and str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from panasonic_posture.linux.service.daemon import main  # noqa: E402  (after sys.path setup)
from panasonic_posture.linux.service.application import ServiceApplication
from panasonic_posture.linux.service.sensor import linux_sample_clock
from panasonic_posture.model import HardwareStatus, SensorInfo, SensorReading


class Sensor:
    info = SensorInfo(ready=True, backend='sysfs', device='integration-fixture')

    def start(self): pass
    def close(self): pass
    def set_prefer_shared(self, value): pass
    def reading(self):
        return SensorReading((0., 0., -9.81), linux_sample_clock(), 'sysfs')


def ready_application(**options):
    # Replace only hardware edges; the real daemon, policy, engine, persistence
    # and authenticated session-bus protocol still run in the child process.
    return CREATE_APPLICATION(
        **options,
        profile=SimpleNamespace(id='integration', support_tier='validated'),
        sensor=Sensor(),
        switch=SimpleNamespace(read=lambda: (False, HardwareStatus.OK)),
        actuator=SimpleNamespace(release=lambda: None),
    )


CREATE_APPLICATION = ServiceApplication.create


def run(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        raise SystemExit("usage: session_daemon.py <config-path>")
    if args[1:] == ['--ready-fixture']:
        with patch.object(ServiceApplication, 'create', side_effect=ready_application):
            return main(["--session-bus", "--config", args[0]])
    if args[1:]:
        raise SystemExit('unknown integration fixture option')
    return main(["--session-bus", "--config", args[0]])


if __name__ == "__main__":
    raise SystemExit(run())
