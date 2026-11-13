"""Shared engine/ports plus the Linux privileged D-Bus service.

``engine``, ``ports``, ``hardware_monitor`` and the settings schema/store are
platform-neutral and are also used by the Windows composition.  ``access``,
``actuator``, ``application``, ``daemon``, ``sensor`` and ``sensor_sources``
are Linux adapters; Windows service code lives under ``panasonic_posture.windows``.
"""
