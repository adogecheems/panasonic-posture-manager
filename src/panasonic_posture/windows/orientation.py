"""Read-only coarse direction identity and a pure dwell policy.

Direction snapshots use the companion accelerometer's mapped clock. They
advance only with a distinct gravity frame, but retain their own observation
time. A repeated cached frame cannot renew direction freshness or dwell.
These values alone never enable a display mode or write the desktop.
"""
from collections.abc import Sequence
from dataclasses import dataclass, replace
import math

from ..model import SensorInfo


# SimpleOrientation reports counter-clockwise device rotation, while the
# display transaction uses clockwise quarter turns.  Face-up/face-down values
# are handled by QuadrantPolicy and never appear in this mapping.
SIMPLE_ORIENTATION_TRANSFORM = (0, 3, 2, 1)


class OrientationError(ValueError):
    pass


@dataclass(frozen=True)
class OrientationReading:
    value: int
    stamp: float
    device: str


def _number(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


class OrientationTracker:
    def __init__(self) -> None:
        self.info = SensorInfo(backend='windows-simple-orientation', error='starting')
        self.latest: OrientationReading | None = None
        self._stamp: float | None = None

    def reset(self, error: str = 'starting') -> None:
        self.latest = None
        self.info = replace(self.info, ready=False, error=error)

    def accept(self, value: object, *, stamp: float) -> OrientationReading:
        try:
            if not isinstance(value, dict):
                raise OrientationError('orientation-sensor-unavailable')
            device = value.get('device')
            if not isinstance(device, str) or not 0 < len(device) <= 2048:
                raise OrientationError('invalid-orientation-identity')
            if value.get('candidates') != [device]:
                raise OrientationError('orientation-sensor-ambiguous')
            if self.info.device and self.info.device != device:
                raise OrientationError('orientation-identity-changed')
            direction = value.get('value')
            if type(direction) is not int or direction not in range(6):
                raise OrientationError('invalid-orientation-value')
            if type(value.get('reading_transform')) is not int or value['reading_transform'] != 0:
                raise OrientationError('orientation-reading-was-transformed')
            if not _number(stamp) or (self._stamp is not None and stamp < self._stamp):
                raise OrientationError('invalid-orientation-clock')
            if stamp == self._stamp:
                if self.latest is None:
                    raise OrientationError('orientation-sample-repeated-after-reset')
                return self.latest
            self.latest = OrientationReading(direction, stamp, device)
            self._stamp = stamp
            self.info = SensorInfo(ready=True, backend='windows-simple-orientation', device=device)
            return self.latest
        except OrientationError as error:
            self.reset(str(error))
            raise


class QuadrantPolicy:
    """Return an explicitly mapped quadrant after distinct stable samples.

Face-up/down retains the caller's current direction. Missing, stale, reordered
or replaced data resets pending decisions. Mapping belongs to a device/display
composition, rather than a guessed raw accelerometer X-axis convention.
"""
    def __init__(self, *, mapping: Sequence[int], settle_seconds: float = .4, max_age: float = 2.) -> None:
        if (not isinstance(mapping, Sequence) or len(mapping) != 4 or any(type(value) is not int for value in mapping) or
                set(mapping) != set(range(4))):
            raise ValueError('an explicit four-quadrant permutation is required')
        if not all(_number(value) and value > 0 for value in (settle_seconds, max_age)):
            raise ValueError('positive finite timing limits are required')
        self.mapping = tuple(mapping)
        self.settle_seconds, self.max_age = settle_seconds, max_age
        self._device = ''
        self.reset()

    def reset(self) -> None:
        self._candidate: int | None = None
        self._since: float | None = None
        self._last: float | None = None
        self._now: float | None = None

    def choose(self, reading: OrientationReading | None, *, now: float) -> int | None:
        if (not isinstance(reading, OrientationReading) or not _number(now) or
                not _number(reading.stamp) or not -.05 <= now - reading.stamp <= self.max_age or
                type(reading.value) is not int or reading.value not in range(6) or
                not isinstance(reading.device, str) or not reading.device or
                (self._device and self._device != reading.device) or
                (self._last is not None and reading.stamp < self._last) or
                (self._now is not None and now < self._now)):
            self.reset()
            raise OrientationError('orientation-data-unavailable-or-changed')
        if self._last is not None and reading.stamp - self._last > self.max_age:
            self.reset()
        self._device, self._last, self._now = reading.device, reading.stamp, now
        if reading.value >= 4:
            self._candidate = self._since = None
            return None
        if reading.value != self._candidate:
            self._candidate, self._since = reading.value, reading.stamp
        return self.mapping[reading.value] if reading.stamp - self._since >= self.settle_seconds else None
