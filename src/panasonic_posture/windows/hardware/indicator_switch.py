"""Fresh read-only samples of the convertible indicator for validated models."""
from collections.abc import Callable
import threading
import time

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.hardware.convertible_slate import NativeSlateDevice
from panasonic_posture.windows.hardware.etw_switch import AcpiPhysicalSwitch
from panasonic_posture.windows.hardware.slate_bootstrap import has_legacy_slate_journal

SAMPLE_INTERVAL = .1
SAMPLE_MAX_AGE = 1.0


class IndicatorSwitch:
    def __init__(self, events: AcpiPhysicalSwitch, *, open_device=None,
                 legacy: Callable[[], bool] = has_legacy_slate_journal, clock=time.monotonic):
        self.events, self.clock, self.legacy = events, clock, legacy
        self.open_device = open_device or (lambda: NativeSlateDevice(read_only=True))
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.epoch = 0
        self.sample: tuple[bool, float] | None = None
        self.legacy_output = False
        self.worker: threading.Thread | None = None

    def start(self) -> None:
        self.events.start()
        self.worker = threading.Thread(target=self._run, name='physical-indicator', daemon=True)
        self.worker.start()

    def _run(self) -> None:
        # An old per-user journal may describe a software-owned PDO. Only
        # independent ETW events can establish physical state in that case.
        try:
            legacy = bool(self.legacy())
        except (OSError, ValueError):
            legacy = True
        if legacy:
            with self.lock:
                self.legacy_output = True
            return
        while not self.stop.is_set():
            try:
                with self.open_device() as device:
                    while not self.stop.is_set():
                        with self.lock:
                            epoch = self.epoch
                        stamp = self.clock()
                        value = device.read()
                        with self.lock:
                            if self.epoch == epoch:
                                self.sample = value, stamp
                        self.stop.wait(SAMPLE_INTERVAL)
            except (OSError, ValueError):
                with self.lock:
                    self.sample = None
                self.stop.wait(1.)

    def query(self) -> tuple[bool | None, HardwareStatus]:
        if self.stop.is_set():
            return None, HardwareStatus.ABSENT
        event, status = self.events.query()
        with self.lock:
            if self.legacy_output or status not in (HardwareStatus.OK, HardwareStatus.ABSENT):
                return event, status
            if self.sample is not None:
                value, stamp = self.sample
                if 0 <= self.clock() - stamp < SAMPLE_MAX_AGE:
                    return value, HardwareStatus.OK
        return None, HardwareStatus.ABSENT

    def invalidate(self) -> None:
        self.events.invalidate()
        with self.lock:
            self.epoch += 1
            self.sample = None

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
        self.events.close()
