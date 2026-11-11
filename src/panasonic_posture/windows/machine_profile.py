"""Verified machine identity and the sensor profile tied to that machine."""
from dataclasses import replace
from typing import Protocol

from ..model import SensorInfo, SensorReading
from .machine_identity import SUPPORTED_WINDOWS_MACHINE, read_machine_identity
from .orientation import OrientationReading


class ProfiledSensor(Protocol):
    @property
    def info(self) -> SensorInfo: ...

    def reading(self) -> SensorReading: ...

    def orientation(self) -> OrientationReading | None: ...

    def start(self) -> None: ...

    def close(self) -> None: ...


class ProfileSensor:
    """Do not apply the known mount to a different sensor model."""

    def __init__(self, sensor: ProfiledSensor) -> None:
        self.sensor = sensor

    @property
    def info(self) -> SensorInfo:
        info = self.sensor.info
        if ((info.ready or info.device) and
                not info.device.casefold().startswith('\\\\?\\hid#vid_8087&pid_0ac2#')):
            return replace(info, ready=False, error='sensor-mount-profile-mismatch')
        return info

    def reading(self) -> SensorReading:
        return self.sensor.reading() if self.info.ready else SensorReading(source='windows-winrt')

    def orientation(self) -> OrientationReading | None:
        return self.sensor.orientation() if self.info.ready else None

    def start(self) -> None:
        self.sensor.start()

    def close(self) -> None:
        self.sensor.close()


def verified_machine() -> tuple[str, str]:
    """Read identity only; capability checks stay in the native session factory."""
    identity = read_machine_identity()
    if identity != SUPPORTED_WINDOWS_MACHINE:
        raise OSError('no verified Windows posture mount for this machine')
    return identity


__all__ = ['ProfileSensor', 'verified_machine']
