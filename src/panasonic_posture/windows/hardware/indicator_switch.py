"""Capability-based physical reads, with event fallback for absent indicators."""
from collections.abc import Callable
import logging
import threading
import time

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.hardware.convertible_slate import NativeSlateDevice, SlateDeviceAbsent
from panasonic_posture.windows.hardware.etw_switch import AcpiPhysicalSwitch

log = logging.getLogger(__name__)

SAMPLE_INTERVAL = .1
SAMPLE_MAX_AGE = 1.0


class IndicatorSwitch:
    def __init__(self, *, open_device=None, clock=time.monotonic,
                 fallback_factory: Callable[[], AcpiPhysicalSwitch] | None = None):
        self.clock = clock
        self.fallback_factory = fallback_factory
        self.events: AcpiPhysicalSwitch | None = None
        self.fallback_failed = False
        self.open_device = open_device or (lambda: NativeSlateDevice(read_only=True))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.epoch = 0
        self.sample: tuple[bool, float] | None = None
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        self.worker = threading.Thread(target=self._run, name='physical-indicator', daemon=True)
        self.worker.start()

    def _run(self) -> None:
        indicator_found = False
        while not self.stop.is_set():
            try:
                with self.open_device() as device:
                    indicator_found = True
                    while not self.stop.is_set():
                        with self.lock:
                            epoch = self.epoch
                        stamp = self.clock()
                        value = device.read()
                        with self.lock:
                            if self.epoch == epoch:
                                self.sample = value, stamp
                        self.stop.wait(SAMPLE_INTERVAL)
            except (OSError, ValueError) as error:
                if (isinstance(error, SlateDeviceAbsent) and not indicator_found
                        and self.fallback_factory is not None):
                    self._start_events()
                    return
                with self.lock:
                    self.sample = None
                self.stop.wait(1.)

    def _start_events(self) -> None:
        # Construction runs off the owner loop. A delayed startup must honor
        # power invalidation and clean up locally if close already returned.
        events = None
        try:
            with self.lock:
                if self.stop.is_set():
                    return
                epoch = self.epoch
            events = self.fallback_factory()
            events.start()
            with self.lock:
                if not self.stop.is_set():
                    if epoch != self.epoch:
                        events.invalidate()
                    self.events = events
                    return
        except (OSError, ValueError):
            with self.lock:
                self.fallback_failed = True
            log.warning('physical event fallback unavailable', exc_info=True)
        if events is not None:
            events.close()

    def query(self) -> tuple[bool | None, HardwareStatus]:
        if self.stop.is_set():
            return None, HardwareStatus.ABSENT
        with self.lock:
            if self.events is not None:
                return self.events.query()
            if self.fallback_failed:
                return None, HardwareStatus.READ_ERROR
            if self.sample is not None:
                value, stamp = self.sample
                if 0 <= self.clock() - stamp < SAMPLE_MAX_AGE:
                    return value, HardwareStatus.OK
        return None, HardwareStatus.ABSENT

    def invalidate(self) -> None:
        with self.lock:
            self.epoch += 1
            self.sample = None
            if self.events is not None:
                self.events.invalidate()

    # A fresh read serves startup and resume; no timeout supplies a posture.
    def bootstrap(self) -> None:
        return

    def finalize_bootstrap(self, fallback: bool) -> None:
        return

    def recover(self) -> None:
        return

    def close(self) -> None:
        self.stop.set()
        if self.worker is not None:
            self.worker.join(timeout=1.)
        with self.lock:
            events, self.events = self.events, None
        if events is not None:
            events.close()
