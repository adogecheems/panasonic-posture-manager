"""Thread-affinity guard shared by mutable Windows UI clients."""
import threading


class OwnerLoop:
    """Bind mutable client state to the thread that starts the client."""

    def __init__(self) -> None:
        self._thread = None

    def bind(self) -> None:
        current = threading.get_ident()
        if self._thread is None:
            self._thread = current
        elif self._thread != current:
            raise RuntimeError('Windows UI client must run on its owner loop')

    def check(self) -> None:
        if self._thread is not None and threading.get_ident() != self._thread:
            raise RuntimeError('Windows UI client must run on its owner loop')


__all__ = ['OwnerLoop']
