"""Fresh read-only samples of the convertible indicator for validated models."""
import threading
import time

from panasonic_posture.model import HardwareStatus
from panasonic_posture.windows.hardware.convertible_slate import NativeSlateDevice

SAMPLE_INTERVAL = .1
SAMPLE_MAX_AGE = 1.0


class IndicatorSwitch:
    def __init__(self, *, open_device=None, clock=time.monotonic):
        self.clock = clock
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
        with self.lock:
            if self.sample is not None:
                value, stamp = self.sample
                if 0 <= self.clock() - stamp < SAMPLE_MAX_AGE:
                    return value, HardwareStatus.OK
        return None, HardwareStatus.ABSENT

    def invalidate(self) -> None:
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
