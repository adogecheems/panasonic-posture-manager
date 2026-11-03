"""Accelerometer acquisition with IIO/sysfs source selection.

The worker thread prefers the IIO buffer (native ~10 Hz stream) and falls back
to polling the IIO raw sysfs attributes when the buffer is unavailable or stops
delivering samples.  The IIO path is retried every ``SENSOR_RETRY_SECONDS`` so a
temporary failure (buffer busy, device re-enumerated) does not degrade the
service permanently.

When the hardware reports tablet mode, or a software posture takeover is
active or awaiting confirmation (``prefer_shared``), the sysfs source is
preferred.  This lets ``iio-sensor-proxy``/GNOME own the buffer for automatic
rotation, and keeps the software takeover fed by a source that this firmware
does not repeatedly stop.  While shared mode is active no periodic IIO probing
happens; leaving it wakes the worker and retries IIO immediately.  If sysfs
itself is unavailable, IIO is still used as a fallback.

While the sysfs fallback is running, the periodic IIO retry is a *shadow*
trial (review Q-04): a helper thread opens the IIO buffer while sysfs keeps
publishing samples.  The manager only switches to IIO after a sustained sample
window, not after the first frame.  Some firmware delivers one frame and then
stalls; treating that as healthy used to stop sysfs for the full IIO timeout,
trip policy staleness and release the software tablet takeover.

The old ``ClaimAccelerometer`` D-Bus call is deliberately *not* used: taking
the buffer directly and claiming the proxy at the same time is what caused the
previous sensor conflicts.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

from ..model import SensorReading, SensorSource
from ..protocol import IIO_NO_SAMPLE_SECONDS, SENSOR_RETRY_SECONDS
from .sensor_sources import (
    DEFAULT_ACCEL_NAME,
    IIO_DEVICE_GLOB,
    SYSFS_SLOW_READ_SECONDS,
    IioSampleSource,
    SampleSource,
    SourceUnavailable,
    SysfsSampleSource,
)

LOG = logging.getLogger(__name__)

SYSFS_RETRY_SECONDS = 1.0
#: Real Intel-ISH sysfs reads on the supported CFQV8 take 0.57-0.66 s.
#: Keep the watchdog comfortably above that normal jitter while staying well
#: below the policy's stale window.
#: Minimum spacing between source reads.  Most sources block naturally, but a
#: source that returns ``None`` immediately must not turn the worker into a
#: busy loop; 20 Hz is also a hard upper bound for duplicate samples.
SENSOR_MIN_READ_INTERVAL_SECONDS = 0.05
#: Bound shutdown waits for a source stuck in an uninterruptible sysfs read.
SENSOR_CLOSE_TIMEOUT_SECONDS = 2.0
#: How long a shadow IIO trial waits for its first sample before giving up.
#: It runs on a helper thread while sysfs keeps feeding samples, so a silent
#: firmware costs no data gap at all (review Q-04).
IIO_TRIAL_SECONDS = 1.0


class _IioTrial:
    """State of one shadow probe of the IIO buffer."""

    __slots__ = ("cancel", "done", "error", "source")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.source: SampleSource | None = None
        self.error: Any = None


class SensorManager:
    """Own accelerometer acquisition on a background thread."""

    def __init__(
        self,
        stop: threading.Event,
        profile: Any,
        managed: bool = True,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] | None = None,
        iio_factory: Callable[[], SampleSource] | None = None,
        sysfs_factory: Callable[[], SampleSource] | None = None,
        logger: logging.Logger = LOG,
    ) -> None:
        self._external_stop = stop
        self._stop = threading.Event()
        self._profile = profile
        self._managed = bool(managed)
        self._monotonic = monotonic
        self._bootclock = clock or (lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
        self._iio_factory = iio_factory
        self._sysfs_factory = sysfs_factory
        self._log = logger
        self._lock = threading.Lock()
        self._reading = SensorReading()
        self._source = SensorSource.STARTING
        self._error: str | None = None
        self._prefer_shared = False
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        # Monotonic time of the next IIO retry.  While sysfs is active this is
        # served by a shadow trial instead of stopping sysfs.
        self._iio_retry_at = 0.0
        self._trial: _IioTrial | None = None
        self._trial_lock = threading.Lock()

    # -- public API ------------------------------------------------------

    def start(self) -> None:
        """Start the acquisition thread (no-op when not ``managed``)."""
        if not self._managed or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._acquire_loop, name="accel-reader", daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Stop the worker and wait for it to release the sensor."""
        self._stop.set()
        self._cancel_trial()
        self._wake.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=SENSOR_CLOSE_TIMEOUT_SECONDS)
            if thread.is_alive():  # pragma: no cover - pathological
                self._log.warning("accelerometer thread did not stop in time")

    def reading(self) -> SensorReading:
        with self._lock:
            return self._reading

    @property
    def source(self) -> SensorSource:
        with self._lock:
            return self._source

    @property
    def sensor_error(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def prefer_shared(self) -> bool:
        with self._lock:
            return self._prefer_shared

    def set_prefer_shared(self, value: bool) -> None:
        """Switch source priority (``True`` while GNOME should auto-rotate)."""
        shared = bool(value)
        with self._lock:
            changed = self._prefer_shared != shared
            self._prefer_shared = shared
        if not changed:
            return
        if shared:
            # GNOME may need the buffer now; never leave a shadow trial behind.
            self._cancel_trial()
        else:
            # Leaving tablet mode: upgrade to IIO without waiting a retry cycle.
            self._iio_retry_at = 0.0
        self._wake.set()

    # -- worker ----------------------------------------------------------

    def _stopped(self) -> bool:
        return self._stop.is_set() or self._external_stop.is_set()

    def _priority(self) -> tuple[str, str]:
        iio = str(SensorSource.IIO)
        sysfs = str(SensorSource.SYSFS)
        return (sysfs, iio) if self.prefer_shared else (iio, sysfs)

    def _acquire_loop(self) -> None:
        self._log.info("accelerometer acquisition started (managed)")
        iio = str(SensorSource.IIO)
        sysfs = str(SensorSource.SYSFS)
        while not self._stopped():
            order = self._priority()
            for index, kind in enumerate(order):
                if self._stopped():
                    return
                if (
                    kind == iio
                    and index > 0
                    and order[0] == iio
                    and self._monotonic() < self._iio_retry_at
                ):
                    # IIO is preferred but recently failed: the sysfs fallback
                    # below retries it as a shadow trial when it is due.
                    continue
                shadow = kind == sysfs and order[0] == iio and not self.prefer_shared
                result, switched = self._run_source(kind, shadow_iio=shadow)
                if result == "stopped":
                    return
                if result == "switch" and switched is not None:
                    # The shadow trial got its first sample (already published)
                    # and handed the open IIO source over; keep reading it on
                    # the manager thread.
                    self._iio_retry_at = float("inf")
                    result, _ = self._run_source(iio, source=switched)
                    if result == "stopped":
                        return
                    self._iio_retry_at = self._monotonic() + SENSOR_RETRY_SECONDS
                    break  # restart priority loop -> sysfs fallback again
                if result == "deadline":
                    break
                if kind == iio:
                    self._iio_retry_at = self._monotonic() + SENSOR_RETRY_SECONDS
                # "failed": try the next source in priority order.
            else:
                # Both sources are unavailable; avoid a hot retry loop.
                self._iio_retry_at = self._monotonic() + SENSOR_RETRY_SECONDS
                self._wake.wait(SYSFS_RETRY_SECONDS)
                self._wake.clear()
        self._log.info("accelerometer acquisition stopped")

    def _make_source(self, kind: str) -> SampleSource:
        if kind == str(SensorSource.IIO):
            if self._iio_factory is not None:
                return self._iio_factory()
            from ..hardware.iio import Accel  # T3 module, imported lazily

            name = getattr(self._profile, "accel_name", None) or None
            mount = getattr(self._profile, "mount_matrix", None)
            return IioSampleSource(Accel(self._external_stop, name, mount))
        if self._sysfs_factory is not None:
            return self._sysfs_factory()
        return SysfsSampleSource(self._profile)

    # -- shadow IIO trial -------------------------------------------------

    def _start_iio_trial(self) -> None:
        trial = _IioTrial()
        with self._trial_lock:
            if self._trial is not None:
                return
            self._trial = trial
        threading.Thread(
            target=self._run_iio_trial,
            args=(trial,),
            name="accel-iio-trial",
            daemon=True,
        ).start()

    def _run_iio_trial(self, trial: _IioTrial) -> None:
        source: SampleSource | None = None
        latest: tuple[float, float, float] | None = None
        sample_count = 0
        first_sample_at: float | None = None
        latest_sample_at: float | None = None
        try:
            source = self._make_source(str(SensorSource.IIO))
            source.open()
            deadline = self._monotonic() + IIO_TRIAL_SECONDS
            while not self._stopped() and not trial.cancel.is_set():
                if self._monotonic() >= deadline:
                    break
                read_started = self._monotonic()
                try:
                    vector = source.read()
                except Exception as error:  # noqa: BLE001 - trial failure is safe
                    trial.error = error
                    break
                if vector is None:
                    timeout = SENSOR_MIN_READ_INTERVAL_SECONDS - (
                        self._monotonic() - read_started
                    )
                    if timeout > 0 and trial.cancel.wait(timeout):
                        break
                    continue
                sample_at = self._monotonic()
                if first_sample_at is None:
                    first_sample_at = sample_at
                latest_sample_at = sample_at
                latest = vector
                sample_count += 1

            # A single sample is not evidence of a usable IIO stream.  This
            # firmware can deliver one frame and then go silent for seconds;
            # handing over on that frame stops the healthy sysfs fallback and
            # leaves the posture policy without samples long enough to release
            # the software tablet takeover.  Require a sustained probation
            # window before publishing anything from the trial.
            healthy = (
                trial.error is None
                and sample_count >= 2
                and first_sample_at is not None
                and latest_sample_at is not None
                and latest_sample_at - first_sample_at >= IIO_TRIAL_SECONDS / 2
            )
            if not healthy:
                if trial.error is None:
                    trial.error = SourceUnavailable(
                        "no sustained sample stream within trial window"
                    )
                return

            # Publish and hand over under the trial lock.  Cancellation must
            # either win before this point (so no stale IIO sample is published)
            # or observe the handed-off source and close it.
            with self._trial_lock:
                if self._trial is not trial or trial.cancel.is_set():
                    return
                # This is the only cross-thread reader; _publish takes the
                # reading lock itself.
                if latest is None:  # pragma: no cover - defensive invariant
                    trial.error = SourceUnavailable(
                        "healthy trial did not retain a sample"
                    )
                    return
                self._publish(str(SensorSource.IIO), latest)
                trial.source = source
                source = None  # ownership passed to the manager thread
        except Exception as error:  # noqa: BLE001 - trials never raise out
            trial.error = error
        finally:
            if source is not None:
                try:
                    source.close()
                except Exception:  # noqa: BLE001 - cleanup must not mask failure
                    self._log.debug("cannot close IIO trial source", exc_info=True)
            # Keep the completed trial reachable until the manager thread
            # consumes it.  Clearing _trial here loses both a successful
            # handover and a failed trial's retry backoff; the manager then
            # immediately starts another trial, rapidly toggling the IIO
            # buffer.  _poll_shadow_trial() owns clearing the reference.
            trial.done.set()

    def _cancel_trial(self) -> None:
        source: SampleSource | None = None
        with self._trial_lock:
            trial = self._trial
            if trial is None:
                return
            self._trial = None
            trial.cancel.set()
            # A trial may already have handed its open source to the manager
            # even before it sets ``done``.  Once trial.source is set the trial
            # thread no longer uses it, so the cancelling thread owns closing
            # it.  Before the handover the trial still owns its source and
            # closes it in _run_iio_trial().
            if trial.source is not None:
                source, trial.source = trial.source, None
        if source is not None:
            try:
                source.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask failure
                self._log.debug("cannot close cancelled IIO source", exc_info=True)

    def _poll_shadow_trial(self) -> SampleSource | None:
        """Start/poll the shadow IIO trial while sysfs keeps running."""
        now = self._monotonic()
        trial = self._trial
        if trial is None:
            if now >= self._iio_retry_at:
                self._start_iio_trial()
                trial = self._trial
            if trial is None:
                return None
        if not trial.done.is_set():
            return None
        with self._trial_lock:
            if self._trial is not trial:
                return None
            self._trial = None
            source, trial.source = trial.source, None
        if source is not None:
            return source
        # Silent firmware / busy buffer: keep sysfs running and try again
        # later.  This is not a service error because the active source is
        # healthy; only log it at info level.
        if trial.error is not None:
            self._log.info("IIO shadow trial failed: %s", trial.error)
        self._iio_retry_at = now + SENSOR_RETRY_SECONDS
        return None

    def _wait_for_cadence(self, started: float) -> str | None:
        """Wait for the next read slot, waking on lifecycle events.

        Returns ``"stopped"`` or ``"deadline"`` when the worker should leave
        the current source, and ``None`` when the next read may start.  The
        interruptible wait also gives ``set_prefer_shared`` and ``close`` a
        bounded response time while a source produces empty samples rapidly.
        """
        timeout = SENSOR_MIN_READ_INTERVAL_SECONDS - (self._monotonic() - started)
        if timeout > 0 and self._wake.wait(timeout):
            if self._stopped():
                return "stopped"
            self._wake.clear()
            return "deadline"
        if self._stopped():
            return "stopped"
        return None

    def _run_source(
        self,
        kind: str,
        *,
        source: SampleSource | None = None,
        shadow_iio: bool = False,
    ) -> tuple[str, SampleSource | None]:
        if source is None:
            try:
                source = self._make_source(kind)
                source.open()
            except Exception as error:  # noqa: BLE001 - any failure means fallback
                self._record_error(kind, error)
                return "failed", None
        self._set_source(kind)
        last_sample = self._monotonic()
        try:
            while True:
                if self._stopped():
                    return "stopped", None
                if self._wake.is_set():
                    self._wake.clear()
                    return "deadline", None
                if shadow_iio:
                    switched = self._poll_shadow_trial()
                    if switched is not None:
                        return "switch", switched
                read_started = self._monotonic()
                try:
                    vector = source.read()
                except Exception as error:  # noqa: BLE001 - source failed, fall back
                    self._record_error(kind, error)
                    return "failed", None
                if vector is None:
                    if self._monotonic() - last_sample >= IIO_NO_SAMPLE_SECONDS:
                        self._record_error(kind, "produced no samples")
                        return "failed", None
                else:
                    last_sample = self._monotonic()
                    self._publish(kind, vector)
                result = self._wait_for_cadence(read_started)
                if result is not None:
                    return result, None
        finally:
            if shadow_iio:
                self._cancel_trial()
            try:
                source.close()
            except Exception:  # noqa: BLE001 - cleanup must not mask failure
                self._log.debug("cannot close sensor source", exc_info=True)

    def _publish(self, kind: str, vector: Any) -> None:
        try:
            values = tuple(float(value) for value in vector)
        except (TypeError, ValueError):
            self._record_error(kind, f"invalid sample {vector!r}")
            return
        if len(values) != 3 or not all(math.isfinite(value) for value in values):
            self._record_error(kind, f"invalid sample {values!r}")
            return
        stamp = self._bootclock()
        with self._lock:
            self._reading = SensorReading(
                vector=values, stamp=stamp, source=SensorSource(kind)
            )
            self._source = SensorSource(kind)
            had_error = self._error is not None
            self._error = None
        if had_error:
            self._log.info("accelerometer %s source recovered", kind)

    def _set_source(self, kind: str) -> None:
        with self._lock:
            self._source = SensorSource(kind)

    def _record_error(self, kind: str, error: Any) -> None:
        text = f"{kind}: {error}"[:200]
        with self._lock:
            changed = self._error != text
            self._error = text
        if changed:
            self._log.warning("accelerometer %s unavailable: %s", kind, error)


__all__ = [
    "DEFAULT_ACCEL_NAME",
    "IIO_DEVICE_GLOB",
    "IIO_NO_SAMPLE_SECONDS",
    "IIO_TRIAL_SECONDS",
    "SENSOR_CLOSE_TIMEOUT_SECONDS",
    "SENSOR_MIN_READ_INTERVAL_SECONDS",
    "SENSOR_RETRY_SECONDS",
    "SYSFS_RETRY_SECONDS",
    "SYSFS_SLOW_READ_SECONDS",
    "IioSampleSource",
    "SampleSource",
    "SensorManager",
    "SourceUnavailable",
    "SysfsSampleSource",
]
