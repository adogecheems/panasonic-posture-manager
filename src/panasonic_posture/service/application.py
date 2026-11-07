"""Linux composition root for the privileged system service.

The D-Bus daemon should only own a connection, dispatch methods and manage
GLib sources.  This module is the one place that knows how the domain,
hardware monitoring, sensor acquisition and privileged actuator fit together.
Keeping construction here makes the wiring testable without importing GI and
prevents the daemon shell from growing another set of dependencies.
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..controller import Controller
from ..hardware.profile import load_profile
from ..hardware.sysfs import SwitchReader
from ..model import StateSnapshot, DeviceInfo
from ..presentation import PosePresentation
from .access import AccessControl
from .actuator import Actuator
from .engine import Engine
from .sensor import SensorManager, linux_sample_clock
from .suspend_touch import SuspendTouchWake
from .settings import DEFAULT_SETTINGS_PATH, SettingsStore

LOG = logging.getLogger(__name__)


@dataclass
class ServiceApplication:
    """Instantiated service collaborators and their lifecycle boundary."""

    profile: Any
    controller: Controller
    presentation: PosePresentation
    settings: SettingsStore
    access: AccessControl
    sensor: Any
    switch: Any
    actuator: Any
    engine: Engine

    @classmethod
    def create(
        cls,
        *,
        stop: threading.Event,
        config_path: str | Path = DEFAULT_SETTINGS_PATH,
        development: bool = False,
        instance: str | None = None,
        on_state: Callable[[StateSnapshot], None] | None = None,
        profile: Any | None = None,
        controller: Controller | None = None,
        presentation: PosePresentation | None = None,
        settings: Any | None = None,
        access: Any | None = None,
        sensor: Any | None = None,
        switch: Any | None = None,
        actuator: Any | None = None,
        touch_wake_policy: SuspendTouchWake | None = None,
    ) -> "ServiceApplication":
        """Build a complete application, allowing each edge to be replaced.

        Production uses the defaults.  Tests may inject fakes without touching
        real hardware, D-Bus, root-only device nodes or the user's settings.
        """
        resolved_profile = profile if profile is not None else load_profile()
        resolved_controller = controller if controller is not None else Controller()
        if resolved_controller.generation == 0:
            # A random starting generation makes feedback from a previous
            # service instance useless instead of accidentally matching.
            resolved_controller.generation = secrets.randbits(31)
        resolved_presentation = (
            presentation if presentation is not None else PosePresentation()
        )
        resolved_settings = settings if settings is not None else SettingsStore(config_path)
        resolved_access = access if access is not None else AccessControl(None)
        resolved_sensor = sensor if sensor is not None else SensorManager(
            stop, resolved_profile, managed=True, clock=linux_sample_clock
        )
        resolved_switch = (
            switch
            if switch is not None
            else SwitchReader(getattr(resolved_profile, "switch_name", None))
        )
        resolved_actuator = (
            actuator if actuator is not None else Actuator(resolved_profile)
        )
        if touch_wake_policy is None and not development:
            touch_wake_policy = SuspendTouchWake()
        engine = Engine(
            controller=resolved_controller,
            sensor=resolved_sensor,
            actuator=resolved_actuator,
            presentation=resolved_presentation,
            settings=resolved_settings,
            touch_wake_policy=touch_wake_policy,
            device=DeviceInfo(
                id=str(getattr(resolved_profile, "id", "generic") or "generic"),
                product=str(getattr(resolved_profile, "product", "") or ""),
                vendor=str(getattr(resolved_profile, "vendor", "") or ""),
                support_tier=str(
                    getattr(resolved_profile, "support_tier", "compatible") or "compatible"
                ),
                platform="linux",
            ),
            hardware_query=resolved_switch.read,
            instance=instance,
            development=development,
            clock=linux_sample_clock,
            on_state=on_state,
        )
        return cls(
            profile=resolved_profile,
            controller=resolved_controller,
            presentation=resolved_presentation,
            settings=resolved_settings,
            access=resolved_access,
            sensor=resolved_sensor,
            switch=resolved_switch,
            actuator=resolved_actuator,
            engine=engine,
        )

    def start(self) -> None:
        self.engine.start()

    def sleep_event(self, suspended: bool) -> None:
        """Invalidate cached devices across suspend before resetting the engine."""
        invalidate = getattr(self.switch, "invalidate", None)
        if invalidate is not None:
            try:
                invalidate()
            except Exception:  # noqa: BLE001 - sleep handling must proceed
                LOG.exception("cannot invalidate the hardware switch cache")
        self.engine.sleep_event(suspended)

    def close(self) -> None:
        """Stop the transaction pipeline and the background sensor thread."""
        try:
            self.engine.shutdown()
        except Exception:  # noqa: BLE001 - continue closing other resources
            LOG.exception("engine shutdown failed")
        try:
            self.sensor.close()
        except Exception:  # noqa: BLE001 - shutdown must always complete
            LOG.exception("sensor shutdown failed")


__all__ = ["ServiceApplication"]
