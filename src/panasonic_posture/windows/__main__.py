"""Run a private, unprivileged software backend over inherited binary pipes."""
import argparse
import logging
import queue
import sys
import threading
import time
from typing import BinaryIO

from .backend import Backend, BackendError
from .protocol import VERSION, MAX_FRAME, MAX_PENDING, ProtocolError, encode, decode
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
        except (OSError, ValueError, queue.Full):
            logging.exception('backend input closed or invalid')
        finally:
            stopped.set()

    def send(value):
        sink.write(encode(dict(value, version=VERSION, instance=backend.instance)))
        sink.flush()

    threading.Thread(target=read, daemon=True, name='posture-backend-reader').start()
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
    return 0


def main(argv=None):
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stdio', action='store_true', required=True)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--simulate', action='store_true')
    parser.add_argument('--scenario', choices=('laptop', 'prompt', 'hardware', 'slow', 'error'), default='laptop')
    options = parser.parse_args(argv)
    if options.scenario != 'laptop' and not options.simulate:
        parser.error('--scenario requires --simulate')
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    try:
        with InstanceLock(options.settings):
            return serve(sys.stdin.buffer, sys.stdout.buffer,
                         Backend(options.settings, simulate=options.simulate, scenario=options.scenario))
    except OSError:
        logging.exception('cannot acquire private backend instance or settings directory')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
