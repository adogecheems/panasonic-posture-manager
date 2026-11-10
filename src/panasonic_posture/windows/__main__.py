"""Run a private user backend over inherited binary pipes."""
import argparse
import logging
import queue
import sys
import threading
import time
from typing import BinaryIO

from .backend import Backend, BackendError
from .diagnostics import configure_logging
from .protocol import (
    VERSION, MAX_FRAME, MAX_PENDING, ProtocolError, encode, encode_startup_failure, decode,
)
from .instance import InstanceLock


def serve(source: BinaryIO, sink: BinaryIO, backend: Backend) -> int:
    inbox = queue.Queue(maxsize=MAX_PENDING)
    stopped = threading.Event()

    def read():
        try:
            while not stopped.is_set():
                frame = source.readline(MAX_FRAME + 1)
                if not frame:
                    break
                value = decode(frame)
                if value.get('type') != 'request' or type(value.get('id')) is not int or value['id'] <= 0:
                    raise ProtocolError('invalid request')
                if not isinstance(value.get('method'), str) or not isinstance(value.get('params'), dict):
                    raise ProtocolError('invalid method')
                inbox.put_nowait(value)
                if value['method'] == 'shutdown' and value['params'] == {}:
                    return  # Do not leave a daemon blocked on stdin at exit.
        except (OSError, ValueError, queue.Full):
            logging.exception('backend input closed or invalid')
        finally:
            stopped.set()

    def send(value):
        sink.write(encode(dict(value, version=VERSION, instance=backend.instance)))
        sink.flush()

    reader = threading.Thread(target=read, daemon=True, name='posture-backend-reader')
    reader.start()
    previous = None
    try:
        while not stopped.is_set():
            backend.advance(time.monotonic())
            state = backend.snapshot()
            if state != previous:
                send({'type': 'state', 'state': state})
                previous = state
            try:
                request = inbox.get(timeout=.05)
            except queue.Empty:
                continue
            if request['method'] == 'shutdown' and request['params'] == {}:
                break  # finally restores the display before the child exits.
            error = None
            try:
                backend.call(request['method'], request['params'])
            except BackendError as caught:
                error = caught.code
            except (ValueError, TypeError, RuntimeError) as caught:
                logging.warning('backend request rejected: %s', caught)
                error = 'internal-error'
            # State precedes completion so the UI refreshes from the accepted
            # preference. An operation response alone never means applied.
            state = backend.snapshot()
            send({'type': 'state', 'state': state})
            previous = state
            send({'type': 'response', 'id': request['id'], 'error': error})
    except (OSError, ValueError):
        logging.exception('backend output closed or invalid')
        return 1
    finally:
        stopped.set()
        backend.close()
        reader.join(timeout=1.)
    return 0


def startup_failure_reason(error: OSError, *, native: bool) -> str:
    text = str(error)
    if 'LaptopSlateExclusiveAccess=0' in text:
        return 'slate-configuration-required'
    if isinstance(error, PermissionError):
        return 'native-session-unavailable'
    return 'native-startup-failed' if native else 'backend-startup-failed'


def main(argv=None):
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdio', action='store_true', required=True)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--native', action='store_true', help='real user-session screen control; no input disabling')
    parser.add_argument('--scenario', choices=('laptop', 'prompt', 'hardware', 'slow', 'error'), default='laptop')
    options = parser.parse_args(argv)
    if options.native and options.simulate:
        parser.error('--native cannot be combined with --simulate')
    if options.native and sys.platform != 'win32':
        parser.error('--native requires Windows')
    if options.scenario != 'laptop' and not options.simulate:
        parser.error('--scenario requires --simulate')
    try:
        if options.native:
            from .screen_session import require_native_user
            require_native_user()  # Do not open a caller-selected lock while elevated.
        configure_logging(options.settings)
        with InstanceLock(options.settings):
            return serve(sys.stdin.buffer, sys.stdout.buffer,
                         Backend(options.settings, simulate=options.simulate, scenario=options.scenario, native=options.native))
    except OSError as error:
        logging.exception('cannot start private user backend')
        try:
            sys.stdout.buffer.write(encode_startup_failure(
                startup_failure_reason(error, native=options.native)))
            sys.stdout.buffer.flush()
        except (OSError, ProtocolError):
            pass
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
