"""Owner-loop UI client for a private, same-user backend subprocess.

Workers only move bounded bytes. ``poll`` publishes validated state and
completes Futures on the UI thread. A broken connection invalidates every
pending operation; reconnect never replays a write.
"""
from concurrent.futures import Future
from collections.abc import Callable
from pathlib import Path
import os
import queue
import subprocess
import sys
import threading
import time

from .protocol import (
    VERSION, MAX_FRAME, MAX_PENDING, STARTUP_REASONS, ProtocolError,
    encode, decode, validate_state,
)


class BackendUnavailable(RuntimeError):
    pass


def settings_path(*, simulate: bool = False, native: bool = False) -> Path:
    if simulate and native:
        raise ValueError('native and simulation settings are separate')
    root = os.environ.get('LOCALAPPDATA')
    if not root or not Path(root).is_absolute():
        raise BackendUnavailable('LOCALAPPDATA is not an absolute directory')
    name = 'simulation.json' if simulate else 'native-settings.json' if native else 'settings.json'
    return Path(root) / 'PanasonicPostureManager' / name


class ProcessClient:
    def __init__(self, path: Path, *, simulate: bool = False, scenario: str = 'laptop',
                 timeout: float = 5.0, clock: Callable[[], float] = time.monotonic, native: bool = False) -> None:
        if native and simulate:
            raise ValueError('native and simulation backends are mutually exclusive')
        self.native = native
        self.path, self.simulate, self.scenario = Path(path), simulate, scenario
        self.timeout, self.clock = timeout, clock
        self.process = None
        self.pending = {}
        self.serial = 0
        self.connection = 0
        self.instance = None
        self.startup_failure = None
        self.stopped = True
        self.retry_at = 0.0
        self.backoff = .5
        self._state = lambda state: None
        self._unavailable = lambda: None
        self._connected = lambda: None

    def start(self, on_state: Callable[[dict], None], on_unavailable: Callable[[], None],
              on_connected: Callable[[], None] | None = None) -> None:
        if not self.stopped:
            raise RuntimeError('client already started')
        self._state, self._unavailable = on_state, on_unavailable
        self._connected = on_connected or (lambda: None)
        self.stopped = False
        self._spawn()

    def _spawn(self) -> None:
        self.connection += 1
        self.instance = None
        self.startup_failure = None
        self.inbox = queue.Queue(maxsize=MAX_PENDING * 2)
        self.outbox = queue.Queue(maxsize=MAX_PENDING)
        self.worker_stop = threading.Event()
        self.workers = []
        self.started_at = self.clock()
        self.last_received_at = self.started_at
        executable = Path(sys.executable)
        if os.name == 'nt':
            # Windows venv executables are redirector processes. Terminating
            # one does not synchronously terminate its real interpreter. Own
            # the interpreter directly so wait() also proves file locks and
            # pipe handles have been released before reconnect or shutdown.
            executable = Path(getattr(sys, '_base_executable', None) or sys.executable)
        if os.name == 'nt' and executable.name.lower() == 'pythonw.exe':
            # The child needs real standard streams, even when the GUI parent
            # was launched without a console. CREATE_NO_WINDOW hides its host.
            executable = executable.with_name('python.exe')
        command = [str(executable), '-m', 'panasonic_posture.windows', '--stdio', '--settings', str(self.path)]
        if os.name == 'nt' and executable != Path(sys.executable):
            # The software backend has no third-party dependencies. Select
            # this exact checkout/installed package even if it lives only in
            # the parent's venv; all paths remain separate argv values.
            bootstrap = ('import runpy,sys;sys.path.insert(0,sys.argv.pop(1));'
                         'runpy.run_module("panasonic_posture.windows",run_name="__main__",alter_sys=True)')
            command = [str(executable), '-c', bootstrap, str(Path(__file__).resolve().parents[2]),
                       '--stdio', '--settings', str(self.path)]
        if self.simulate:
            command += ['--simulate', '--scenario', self.scenario]
        if self.native:
            command += ['--native']
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        try:
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags, bufsize=0)
        except OSError:
            self.process = None
            self._lost()
            return
        process, inbox, outbox, stop = self.process, self.inbox, self.outbox, self.worker_stop

        def enqueue(value):
            # Back-pressure bounds memory even if the UI stops polling.
            while not stop.is_set():
                try:
                    inbox.put(value, timeout=.1)
                    return
                except queue.Full:
                    continue

        def read():
            try:
                while not stop.is_set():
                    frame = process.stdout.readline(MAX_FRAME + 1)
                    if not frame:
                        break
                    enqueue(decode(frame))
            except (OSError, ValueError) as error:
                enqueue(error)
            finally:
                enqueue(BackendUnavailable('backend disconnected'))

        def write():
            try:
                while not stop.is_set():
                    try:
                        frame = outbox.get(timeout=.1)
                    except queue.Empty:
                        continue
                    remaining = memoryview(frame)
                    while remaining:
                        written = process.stdin.write(remaining)
                        if not written:
                            raise OSError('backend pipe closed')
                        remaining = remaining[written:]
                    process.stdin.flush()
            except (OSError, ValueError) as error:
                enqueue(error)

        self.workers = [threading.Thread(target=task, daemon=True, name='posture-ipc-' + name)
                        for task, name in ((read, 'reader'), (write, 'writer'))]
        for worker in self.workers:
            worker.start()

    def _dispose(self) -> None:
        if self.process is None:
            return
        process, self.process = self.process, None
        if self.native and process.poll() is None:
            # Keep the writer alive long enough to deliver a normal shutdown.
            # Waiting for process exit also waits for journal recovery/cleanup.
            try:
                self.serial += 1
                self.outbox.put_nowait(encode(dict(version=VERSION, type='request', id=self.serial,
                                                  method='shutdown', params={})))
                process.wait(timeout=6.)
            except (queue.Full, subprocess.TimeoutExpired):
                pass  # A failed child leaves its durable recovery journal.
        self.worker_stop.set()
        # Terminate any remaining process before closing streams; closing a
        # pipe while another thread is blocked reading it can block the UI.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=.5)
        for worker in self.workers:
            worker.join(timeout=.2)
        process.stdin.close()
        process.stdout.close()

    def _lost(self) -> None:
        self.instance = None
        # Invalidate UI connection-scoped callbacks before failing Futures.
        self._unavailable()
        self._dispose()
        pending, self.pending = self.pending, {}
        for future, _ in pending.values():
            if not future.done():
                future.set_exception(BackendUnavailable('backend disconnected'))
        self.retry_at = self.clock() + self.backoff
        self.backoff = min(10.0, self.backoff * 2)

    def poll(self) -> None:
        if self.stopped:
            return
        if self.process is None:
            if self.startup_failure is not None:
                return
            if self.clock() >= self.retry_at:
                self._spawn()
            return
        try:
            for _ in range(MAX_PENDING * 2):
                try:
                    message = self.inbox.get_nowait()
                except queue.Empty:
                    break
                if isinstance(message, Exception):
                    raise message
                kind = message.get('type')
                if kind == 'fatal':
                    reason = message.get('reason')
                    if (set(message) != {'version', 'type', 'reason'} or
                            reason not in STARTUP_REASONS):
                        raise ProtocolError('invalid startup failure')
                    self.startup_failure = reason
                    raise BackendUnavailable('backend startup failed')
                instance = message.get('instance')
                if not isinstance(instance, str) or not instance:
                    raise ProtocolError('missing instance')
                if kind == 'state':
                    state = validate_state(message.get('state'), native=self.native)
                    if instance != state['instance'] or self.instance not in (None, instance):
                        raise ProtocolError('stale instance')
                    first = self.instance is None
                    self.instance = instance
                    if first:
                        self.backoff = .5
                        self._connected()
                    self._state(state)
                elif kind == 'response':
                    serial, error = message.get('id'), message.get('error')
                    if (instance != self.instance or type(serial) is not int or serial not in self.pending
                            or error is not None and not isinstance(error, str)):
                        raise ProtocolError('invalid response')
                    future, _ = self.pending.pop(serial)
                    if not future.done():
                        future.set_exception(RuntimeError(error)) if error else future.set_result(None)
                else:
                    raise ProtocolError('unknown message')
                self.last_received_at = self.clock()
            now = self.clock()
            if self.instance is None and now - self.started_at > self.timeout:
                raise BackendUnavailable('startup timeout')
            if any(now >= deadline for _, deadline in self.pending.values()):
                raise BackendUnavailable('request timeout')
            if self.process is not None and self.process.poll() is not None:
                raise BackendUnavailable('backend exited')
            if (self.instance is not None and not self.pending and
                    now - self.last_received_at >= self.timeout):
                # An open pipe does not prove that the backend loop is alive.
                # Reuse a read-only request and its normal timeout/reconnect
                # path; no preference or mode operation is replayed.
                self.refresh()
        except (OSError, ValueError, BackendUnavailable):
            self._lost()

    def request(self, method: str, params: dict) -> Future:
        future = Future()
        if self.stopped or self.process is None or self.instance is None or len(self.pending) >= MAX_PENDING:
            future.set_exception(BackendUnavailable('backend not ready or queue full'))
            return future
        self.serial += 1
        try:
            frame = encode(dict(version=VERSION, type='request', id=self.serial, method=method, params=params))
            self.outbox.put_nowait(frame)
        except (ValueError, queue.Full) as error:
            future.set_exception(error)
            return future
        self.pending[self.serial] = (future, self.clock() + self.timeout)
        return future

    def refresh(self) -> None:
        if getattr(self, '_refresh', None) is None or self._refresh.done():
            self._refresh = self.request('get_state', {})

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        self._lost()

    def set_mode(self, mode: str) -> Future:
        return self.request('set_mode', {'mode': mode})

    def set_preference(self, field: str, enabled: bool) -> Future:
        return self.request('set_preference', {'field': field, 'enabled': enabled})

    def confirm_prompt(self, prompt: str, epoch: int) -> Future:
        return self.request('confirm_prompt', {'prompt': prompt, 'epoch': epoch})

    def calibrate(self) -> Future:
        return self.request('calibrate', {})

    def reset_calibration(self) -> Future:
        return self.request('reset_calibration', {})

    def retry(self, fallback_mode: str | None = None) -> Future:
        return self.request('retry', {'fallback_mode': fallback_mode})
