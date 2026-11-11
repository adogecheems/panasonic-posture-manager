"""Windows Service Control Manager host for the hardware runtime.

The service executable is the restricted ``python-hardware.exe``.  This
module only adapts the existing :class:`HardwareRuntime` lifecycle to SCM
status and stop notifications; hardware permissions and composition remain
owned by their existing modules.
"""
from __future__ import annotations

import argparse
import ctypes as C
from collections.abc import Callable
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import threading

from ..contracts import TICK_SECONDS
from .hardware_runtime import HardwareRuntime
from .trusted_runtime import TrustedRuntime

log = logging.getLogger(__name__)

SERVICE_NAME = 'PanasonicPostureManagerHardware'
SERVICE_DISPLAY_NAME = 'Panasonic Posture Manager Hardware Service'
SERVICE_DESCRIPTION = (
    'Provides the authenticated LocalSystem hardware endpoint for '
    'Panasonic Posture Manager.'
)
LOG_RELATIVE_PATH = Path('PanasonicPostureManager/Logs/hardware-service.log')

SERVICE_WIN32_OWN_PROCESS = 0x10
SERVICE_ACCEPT_STOP = 0x1
SERVICE_ACCEPT_SHUTDOWN = 0x4
SERVICE_CONTROL_STOP = 0x1
SERVICE_CONTROL_INTERROGATE = 0x4
SERVICE_CONTROL_SHUTDOWN = 0x5
SERVICE_STOPPED = 0x1
SERVICE_START_PENDING = 0x2
SERVICE_STOP_PENDING = 0x3
SERVICE_RUNNING = 0x4
NO_ERROR = 0
ERROR_SERVICE_SPECIFIC_ERROR = 1066
SERVICE_START_WAIT_MS = 30_000
SERVICE_STOP_WAIT_MS = 20_000

U32 = C.c_uint32
LPVOID = C.c_void_p


class ServiceStatus(C.Structure):
    _fields_ = (
        ('service_type', U32),
        ('current_state', U32),
        ('controls_accepted', U32),
        ('win32_exit_code', U32),
        ('service_specific_exit_code', U32),
        ('check_point', U32),
        ('wait_hint', U32),
    )


ServiceMainFunction = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(
    None, U32, C.POINTER(C.c_wchar_p)
)
ServiceHandlerFunction = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(
    U32, U32, U32, LPVOID, LPVOID
)


class ServiceTableEntry(C.Structure):
    _fields_ = (
        ('service_name', C.c_wchar_p),
        ('service_proc', ServiceMainFunction),
    )


ReportStatus = Callable[..., None]


class HardwareServiceRunner:
    """Run and clean up one hardware runtime under a service report callback."""

    def __init__(
        self,
        runtime_factory: Callable[[], HardwareRuntime],
        *,
        tick: float = TICK_SECONDS,
        logger: logging.Logger | None = None,
    ) -> None:
        if not 0 < tick <= 1:
            raise ValueError('service polling interval must be within one second')
        self.runtime_factory = runtime_factory
        self.tick = float(tick)
        self.logger = logger or log

    def run(self, stop: threading.Event, report: ReportStatus) -> int:
        runtime: HardwareRuntime | None = None
        exit_code = NO_ERROR
        report(
            SERVICE_START_PENDING,
            controls=0,
            wait_hint=SERVICE_START_WAIT_MS,
            checkpoint=1,
        )
        try:
            runtime = self.runtime_factory()
            report(
                SERVICE_START_PENDING,
                controls=0,
                wait_hint=SERVICE_START_WAIT_MS,
                checkpoint=2,
            )
            runtime.start()
            report(
                SERVICE_RUNNING,
                controls=SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN,
            )
            while not stop.is_set():
                runtime.poll()
                if stop.wait(self.tick):
                    break
        except BaseException:
            self.logger.exception('hardware service runtime failed')
            exit_code = ERROR_SERVICE_SPECIFIC_ERROR
        finally:
            report(
                SERVICE_STOP_PENDING,
                controls=SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN,
                wait_hint=SERVICE_STOP_WAIT_MS,
                checkpoint=1,
            )
            if runtime is not None:
                try:
                    runtime.close()
                except BaseException:
                    self.logger.exception('hardware service cleanup failed')
                    exit_code = ERROR_SERVICE_SPECIFIC_ERROR
            report(
                SERVICE_STOPPED,
                controls=0,
                win32_exit_code=exit_code,
                service_specific_exit_code=1 if exit_code else 0,
            )
        return exit_code


class WindowsServiceHost:
    """Thin SCM adapter around :class:`HardwareServiceRunner`."""

    def __init__(self, runtime_factory: Callable[[], HardwareRuntime] | None = None) -> None:
        self.stop = threading.Event()
        self.runtime_factory = runtime_factory or HardwareRuntime.installed
        self.runner = HardwareServiceRunner(self.runtime_factory)
        self.handle: int | None = None
        self.state = SERVICE_START_PENDING
        self.status = ServiceStatus(
            service_type=SERVICE_WIN32_OWN_PROCESS,
            current_state=SERVICE_START_PENDING,
            controls_accepted=0,
            win32_exit_code=NO_ERROR,
            service_specific_exit_code=0,
            check_point=1,
            wait_hint=SERVICE_START_WAIT_MS,
        )
        self._status_lock = threading.Lock()
        self.advapi = C.WinDLL('advapi32', use_last_error=True)
        self.advapi.RegisterServiceCtrlHandlerExW.restype = LPVOID
        self.advapi.RegisterServiceCtrlHandlerExW.argtypes = (
            C.c_wchar_p,
            ServiceHandlerFunction,
            LPVOID,
        )
        self.advapi.SetServiceStatus.restype = C.c_int
        self.advapi.SetServiceStatus.argtypes = (LPVOID, C.POINTER(ServiceStatus))
        self._handler = ServiceHandlerFunction(self.on_control)
        self._handler_ref = self._handler

    def report(
        self,
        state: int,
        *,
        controls: int = 0,
        win32_exit_code: int = NO_ERROR,
        service_specific_exit_code: int = 0,
        wait_hint: int = 0,
        checkpoint: int = 0,
    ) -> None:
        with self._status_lock:
            self.state = state
            self.status = ServiceStatus(
                service_type=SERVICE_WIN32_OWN_PROCESS,
                current_state=state,
                controls_accepted=controls,
                win32_exit_code=win32_exit_code,
                service_specific_exit_code=service_specific_exit_code,
                check_point=checkpoint,
                wait_hint=wait_hint,
            )
            if self.handle is None or not self.advapi.SetServiceStatus(
                self.handle, C.byref(self.status)
            ):
                raise OSError(C.get_last_error(), 'cannot report hardware service status')

    def on_control(self, control: int, event_type: int, event_data: int, context: int) -> int:
        del event_type, event_data, context
        if control in (SERVICE_CONTROL_STOP, SERVICE_CONTROL_SHUTDOWN):
            if not self.stop.is_set():
                self.stop.set()
                try:
                    self.report(
                        SERVICE_STOP_PENDING,
                        controls=SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN,
                        wait_hint=SERVICE_STOP_WAIT_MS,
                        checkpoint=1,
                    )
                except OSError:
                    log.exception('cannot report hardware service stop')
                    return C.get_last_error()
        elif control == SERVICE_CONTROL_INTERROGATE:
            with self._status_lock:
                if not self.advapi.SetServiceStatus(self.handle, C.byref(self.status)):
                    return C.get_last_error()
        return NO_ERROR

    def run(self) -> int:
        self.handle = self.advapi.RegisterServiceCtrlHandlerExW(
            SERVICE_NAME, self._handler, None
        )
        if not self.handle:
            error = C.get_last_error()
            log.error('cannot register hardware service control handler: %s', error)
            return error
        return self.runner.run(self.stop, self.report)


_host: WindowsServiceHost | None = None


def _service_main(argc: int, argv) -> None:
    del argc, argv
    host = _host
    if host is None:
        return
    host.run()


def _service_control(control: int, event_type: int, event_data: int, context: int) -> int:
    host = _host
    if host is None:
        return NO_ERROR
    return host.on_control(control, event_type, event_data, context)


_service_main_ref = ServiceMainFunction(_service_main)
_service_control_ref = ServiceHandlerFunction(_service_control)


def configure_logging() -> None:
    """Write bounded service diagnostics beside protected ProgramData state."""
    root = Path(os.environ.get('ProgramData', r'C:\ProgramData'))
    path = root / LOG_RELATIVE_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=256 * 1024, backupCount=2, encoding='utf-8'
        )
    except OSError:
        log.addHandler(logging.NullHandler())
        return
    handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s %(name)s: %(message)s'
    ))
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)


def service_image_path(root: Path) -> str:
    """Return the exact SCM ImagePath for an installed bundle."""
    executable = Path(root).absolute() / 'runtime/python-hardware.exe'
    if any(char in str(executable) for char in '\0\r\n"'):
        raise ValueError('runtime path cannot be represented as an SCM command')
    return (
        f'"{executable}" -B -I -m '
        'panasonic_posture.windows.hardware_service --serve'
    )


def verify_installed_runtime() -> None:
    """Pin and verify the protected installation without starting hardware."""
    runtime = TrustedRuntime.current()
    runtime.close()


def dispatch_service() -> int:
    global _host
    configure_logging()
    _host = WindowsServiceHost()
    table = (ServiceTableEntry * 2)()
    table[0].service_name = SERVICE_NAME
    table[0].service_proc = _service_main_ref
    _host.advapi.StartServiceCtrlDispatcherW.restype = C.c_int
    _host.advapi.StartServiceCtrlDispatcherW.argtypes = (C.POINTER(ServiceTableEntry),)
    if not _host.advapi.StartServiceCtrlDispatcherW(table):
        return C.get_last_error()
    return NO_ERROR


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--serve', action='store_true', help='run under the Windows SCM')
    group.add_argument('--verify', action='store_true', help='verify the installed protected runtime')
    group.add_argument('--image-path', action='store_true', help='print the exact SCM ImagePath')
    options = parser.parse_args(argv)
    if options.verify:
        verify_installed_runtime()
        return 0
    if options.image_path:
        print(service_image_path(Path(__file__).absolute().parents[3]))
        return 0
    return dispatch_service()


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = [
    'ERROR_SERVICE_SPECIFIC_ERROR', 'HardwareServiceRunner', 'LOG_RELATIVE_PATH',
    'SERVICE_DESCRIPTION', 'SERVICE_DISPLAY_NAME', 'SERVICE_NAME',
    'SERVICE_RUNNING', 'SERVICE_START_PENDING', 'SERVICE_STOPPED',
    'SERVICE_STOP_PENDING', 'WindowsServiceHost', 'configure_logging',
    'service_image_path', 'verify_installed_runtime',
]
