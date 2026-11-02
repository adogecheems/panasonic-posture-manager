"""Hardware-switch observation and transient-failure debouncing.

Device discovery belongs to :mod:`panasonic_posture.hardware.sysfs`; this
module owns the service-level policy for invalid or temporarily unreadable
values.  Keeping it separate from the transaction engine makes the safety
behaviour directly testable and ensures every consumer sees the same raw,
effective and status values for a tick.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..model import HardwareStatus
from ..protocol import HARDWARE_UNKNOWN_DEBOUNCE_SECONDS

LOG = logging.getLogger(__name__)


def as_hardware_status(value: Any) -> HardwareStatus:
    """Normalize an injected status without letting a bad value leak out."""
    try:
        return HardwareStatus(value)
    except (TypeError, ValueError):
        return HardwareStatus.READ_ERROR


@dataclass(frozen=True)
class HardwareObservation:
    """One debounced hardware-switch observation.

    ``raw`` is the latest valid boolean (or ``None``); ``effective`` is the
    value the policy may act on during the debounce window; ``status`` always
    describes the raw read so the UI can warn about a flaky device without
    prematurely releasing software tablet control.
    """

    raw: bool | None
    effective: bool | None
    status: HardwareStatus


class HardwareMonitor:
    """Turn a raw switch query into a debounced :class:`HardwareObservation`."""

    def __init__(
        self,
        query: Callable[[], tuple[bool | None, Any]],
        *,
        debounce_seconds: float = HARDWARE_UNKNOWN_DEBOUNCE_SECONDS,
        logger: logging.Logger = LOG,
    ) -> None:
        self._query = query
        self._debounce_seconds = max(0.0, float(debounce_seconds))
        self._log = logger
        self._last_valid: bool | None = None
        self._unknown_since: float | None = None
        self._status = HardwareStatus.ABSENT

    @property
    def last_valid(self) -> bool | None:
        return self._last_valid

    @property
    def status(self) -> HardwareStatus:
        return self._status

    def reset(self) -> None:
        """Drop all history, e.g. when resuming from suspend."""
        self._last_valid = None
        self._unknown_since = None
        self._status = HardwareStatus.ABSENT

    def poll(self, now: float) -> HardwareObservation:
        try:
            raw, status_value = self._query()
        except Exception:  # noqa: BLE001 - switch failure degrades to unknown
            self._log.exception("hardware switch query failed")
            raw, status_value = None, HardwareStatus.READ_ERROR

        self._status = as_hardware_status(status_value)
        if raw is not None and not isinstance(raw, bool):
            self._log.warning("ignoring non-boolean hardware value %r", raw)
            raw = None
        if raw is None:
            if self._unknown_since is None:
                self._unknown_since = now
            if (
                self._last_valid is not None
                and now - self._unknown_since < self._debounce_seconds
            ):
                return HardwareObservation(raw, self._last_valid, self._status)
            return HardwareObservation(None, None, self._status)

        self._unknown_since = None
        self._last_valid = raw
        return HardwareObservation(raw, raw, self._status)


__all__ = [
    "HardwareMonitor",
    "HardwareObservation",
    "as_hardware_status",
]
