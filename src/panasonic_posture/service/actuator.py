"""Bounded ownership of the privileged relay subprocess.

The relay is started on demand when the service wants to take software control
of the tablet switch, kept alive with a heartbeat byte stream and released as
soon as the target no longer needs it.  The engine runs release on a dedicated
single-worker executor and serializes keep/retry around it, so a slow recovery
does not stall the 100 ms tick.  Every blocking operation has an explicit
timeout and the actuator has no GLib dependency.
"""

from __future__ import annotations

import json
import logging
import os
import select
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..protocol import (
    RELAY_HEARTBEAT_SECONDS,
    RELAY_INSTALL_PATH,
    RELAY_PROTOCOL,
    RELAY_REPORT_TIMEOUT_SECONDS,
    ErrorCode,
)
from .ports import ActuatorError as ActuatorError
from ..model import Target

LOG = logging.getLogger(__name__)

ENV_RELAY = "PANASONIC_POSTURE_RELAY"

# Bounded even when an operation is invoked synchronously (for example during
# shutdown or a test with an inline executor).
RELEASE_TIMEOUT_SECONDS = 1.5
RECOVER_TIMEOUT_SECONDS = 1.5


@dataclass(frozen=True)
class RelayState:
    """Parsed ``kind=applied`` report from the relay."""

    tablet: bool
    input_disabled: bool
    hardware: bool
    physical: bool
    error: str | None = None

    def satisfies(self, target: Target) -> bool:
        # Linux needs both the virtual tablet switch and the input grabs.
        # Neither a partial report nor a report carrying an error confirms it.
        return (self.error is None and target.disable_internal_input
                and target.tablet is True and self.tablet and self.input_disabled)

    @classmethod
    def from_message(cls, payload: Mapping[str, Any]) -> RelayState:
        error = payload.get("error")
        return cls(
            tablet=bool(payload.get("tablet")),
            input_disabled=bool(payload.get("input_disabled")),
            hardware=bool(payload.get("hardware")),
            physical=bool(payload.get("physical")),
            error=error if isinstance(error, str) and error else None,
        )


def _profile_executable_args(profile: Any) -> list[str]:
    if profile is None:
        return []
    arguments: list[str] = []
    for option, attribute in (
        ("--switch-name", "switch_name"),
        ("--keyboard-name", "keyboard_name"),
        ("--touchpad-name", "touchpad_name"),
    ):
        value = getattr(profile, attribute, None)
        if value:
            arguments.extend((option, str(value)))
    return arguments


class Actuator:
    """Manage the relay subprocess for software tablet control."""

    def __init__(
        self,
        profile: Any = None,
        *,
        executable: str | os.PathLike[str] | None = None,
        environ: Mapping[str, str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        logger: logging.Logger = LOG,
        popen: Callable[..., Any] | None = None,
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.profile = profile
        self._environ = os.environ if environ is None else environ
        self._monotonic = monotonic
        self._log = logger
        self._popen = subprocess.Popen if popen is None else popen
        self._run = subprocess.run if runner is None else runner
        self.executable = self._resolve_executable(executable)
        self.process: Any = None
        self.state: RelayState | None = None
        self.physical: bool | None = None
        self.error_code: str | None = None
        self.error_detail: str | None = None
        self.last_sent: float | None = None
        self.last_report: float | None = None
        self._buffer = b""
        # A relay that exits on EOF runs its own cleanup and emits the native
        # switch state, so no separate --recover is needed.  Recovery is only
        # required when the relay had to be killed, or when a previous instance
        # died from a signal (its cleanup could not run).  Flagging it here also
        # makes the recovery happen exactly once, before the next spawn.
        self._needs_recovery = False

    # -- paths / commands ------------------------------------------------

    def _resolve_executable(self, explicit: str | os.PathLike[str] | None) -> Path:
        if explicit:
            return Path(explicit)
        override = self._environ.get(ENV_RELAY)
        if override:
            return Path(override)
        installed = Path(RELAY_INSTALL_PATH)
        if installed.is_file():
            return installed
        # Repository checkout: src/panasonic_posture/service/actuator.py
        return Path(__file__).resolve().parents[3] / "backend" / "relay"

    def command(self, recover: bool = False) -> list[str]:
        """Return the argv used to start (or recover) the relay."""
        arguments = [str(self.executable)]
        arguments.extend(_profile_executable_args(self.profile))
        if recover:
            arguments.append("--recover")
        return arguments

    @property
    def running(self) -> bool:
        process = self.process
        return process is not None and process.poll() is None

    # -- lifecycle -------------------------------------------------------

    def keep(self, now: float) -> None:
        """Ensure a healthy relay process and refresh its software lease."""
        if self.error_code is not None:
            raise ActuatorError(self.error_code, self.error_detail)
        if self.process is not None:
            self._read_reports(now)
            if self.error_code is not None:
                raise ActuatorError(self.error_code, self.error_detail)
            self._check_report_timeout(now)
            self._heartbeat(now)
            return
        self._spawn(now)
        if self.error_code is not None:
            raise ActuatorError(self.error_code, self.error_detail)

    def release(self) -> None:
        """Release software control and repair native state only if needed.

        A relay exiting after EOF runs its cleanup, including emitting the
        current physical switch state and destroying the virtual device, so an
        extra ``--recover`` would re-create the virtual device and emit another
        ON/final sequence (visible as a touch-mode flicker on every mode
        change).  Recovery is therefore limited to unsafe terminations: a
        timeout (we kill the relay, so its cleanup could not run), a previous
        signal death flagged by :meth:`_handle_exit`, or a non-zero cleanup
        exit (the final switch state may not have been written).
        """
        process, self.process = self.process, None
        self.state = None
        self.physical = None
        self._buffer = b""
        self.last_sent = None
        self.last_report = None
        if process is None:
            if self._needs_recovery:
                self.recover()
            return
        returncode: int | None = None
        killed = False
        self._close_stdin(process)
        try:
            returncode = self._wait(process, RELEASE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self._log.warning("relay did not exit within %.1fs; killing", RELEASE_TIMEOUT_SECONDS)
            self._kill(process)
            killed = True
            try:
                returncode = self._wait(process, RELEASE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:  # pragma: no cover - pathological
                self._log.error("relay survived SIGKILL")
        except (OSError, ValueError) as error:
            self._log.error("cannot wait for relay release: %s", error)
            self._kill(process)
            killed = True
        finally:
            self._close_streams(process)
        if returncode not in (None, 0):
            self._log.warning(
                "relay cleanup exited with status %s; recovering", returncode
            )
            self._needs_recovery = True
        if killed or self._needs_recovery:
            self.recover()

    close = release

    def recover(self) -> bool:
        """Run ``relay --recover`` so native switches never stay overridden."""
        try:
            completed = self._run(
                self.command(recover=True),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=RECOVER_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self._needs_recovery = True
            self._fail_recovery(str(error))
            return False
        returncode = getattr(completed, "returncode", 1)
        if returncode != 0:
            self._needs_recovery = True
            self._fail_recovery(f"relay --recover exited with status {returncode}")
            return False
        self._needs_recovery = False
        return True

    def retry(self) -> None:
        """Drop a failed process and clear the error latch for a new attempt.

        A failed native-state recovery is still actionable: keep its latched
        error so the caller cannot mistake an unsafe switch state for success.
        """
        self.release()
        if self._needs_recovery:
            return
        self.error_code = None
        self.error_detail = None
        self.state = None

    # -- process plumbing ------------------------------------------------

    def _spawn(self, now: float) -> None:
        try:
            process = self._popen(
                self.command(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,  # relay diagnostics go to the service journal
                bufsize=0,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self._fail(ErrorCode.RELAY_START, str(error))
            self._log.error("cannot start relay %s: %s", self.executable, error)
            return
        self.process = process
        self.state = None
        self.physical = None
        self._buffer = b""
        self.last_sent = None
        self.last_report = now
        self._heartbeat(now)
        self._log.info("started relay %s", self.executable)

    def _read_reports(self, now: float) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            descriptor = process.stdout.fileno()
        except (OSError, ValueError):
            return
        try:
            while True:
                readable, _, _ = select.select([descriptor], [], [], 0)
                if not readable:
                    break
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    self._handle_exit(process)
                    return
                self._buffer += chunk
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    self._accept_report(line, now)
                    if self.process is not process or self.error_code is not None:
                        # The process failed while a report was being parsed.
                        return
        except (OSError, ValueError) as error:
            self._fail(ErrorCode.RELAY_EXITED, f"relay pipe error: {error}")
            self._log.error("relay pipe error: %s", error)

    def _accept_report(self, line: bytes, now: float) -> None:
        text = line.strip()
        if not text:
            return
        try:
            payload = json.loads(text.decode("utf-8", "replace"))
        except ValueError:
            self._log.warning("ignoring malformed relay report: %r", text[:120])
            return
        if not isinstance(payload, dict):
            self._log.warning("ignoring non-object relay report: %r", text[:120])
            return
        if payload.get("protocol") != RELAY_PROTOCOL:
            self._log.warning(
                "ignoring relay report with protocol %r", payload.get("protocol")
            )
            return
        self.last_report = now
        kind = payload.get("kind")
        if kind == "applied":
            self.state = RelayState.from_message(payload)
            self.physical = self.state.physical
        elif kind == "physical":
            self.physical = bool(payload.get("present"))
        elif kind == "ready":
            self.physical = bool(payload.get("physical"))
            self._log.info("relay ready (physical=%s)", self.physical)
        elif kind == "released":
            self.state = None
            self._log.info("relay reported release (exit=%s)", payload.get("exit"))
        else:
            self._log.debug("ignoring relay report kind %r", kind)

    def _check_report_timeout(self, now: float) -> None:
        if self.last_report is None or now - self.last_report > RELAY_REPORT_TIMEOUT_SECONDS:
            self._fail(
                ErrorCode.RELAY_HEARTBEAT,
                f"no relay report for {now - (self.last_report or now):.1f}s",
            )
            raise ActuatorError(
                self.error_code or ErrorCode.RELAY_HEARTBEAT, self.error_detail
            )

    def _heartbeat(self, now: float) -> None:
        process = self.process
        if process is None or process.stdin is None:
            return
        if self.last_sent is not None and now - self.last_sent < RELAY_HEARTBEAT_SECONDS:
            return
        try:
            process.stdin.write(b"1\n")
            process.stdin.flush()
        except (OSError, ValueError) as error:
            self._fail(ErrorCode.RELAY_EXITED, f"relay stdin: {error}")
            self._log.error("cannot heartbeat relay: %s", error)
            return
        self.last_sent = now

    def _handle_exit(self, process: Any) -> None:
        if self.process is not process:
            return
        try:
            code = process.wait(timeout=0.2)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            code = None
        except (OSError, ValueError):  # pragma: no cover - defensive
            code = None
        self.state = None
        # A negative exit status means the relay was killed by a signal: its
        # cleanup (which repairs the native switch state) did not run.  A
        # normal non-zero exit comes from relay's own cleanup path and needs no
        # extra --recover.
        self._needs_recovery = code is None or (isinstance(code, int) and code < 0)
        self._fail(ErrorCode.RELAY_EXITED, f"relay exited with status {code}")
        self._log.error("relay exited unexpectedly with status %s", code)

    # -- small helpers ---------------------------------------------------

    def _fail(self, code: str, detail: str | None) -> None:
        if self.error_code is None:
            self.error_code = code
            self.error_detail = detail

    def _fail_recovery(self, detail: str | None) -> None:
        """Surface an unsafe native-state recovery over an earlier relay error."""

        self.error_code = ErrorCode.NATIVE_RECOVERY
        self.error_detail = detail

    @staticmethod
    def _close_stdin(process: Any) -> None:
        stream = getattr(process, "stdin", None)
        if stream is None:
            return
        try:
            stream.close()
        except (OSError, ValueError):
            pass

    @staticmethod
    def _close_streams(process: Any) -> None:
        for name in ("stdin", "stdout", "stderr"):
            stream = getattr(process, name, None)
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    @staticmethod
    def _wait(process: Any, timeout: float) -> int:
        return process.wait(timeout=timeout)

    @staticmethod
    def _kill(process: Any) -> None:
        try:
            process.kill()
        except (OSError, ValueError):
            pass
