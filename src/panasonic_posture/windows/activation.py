"""One native UI per user, with a coalesced show-window request.

The empty request file is a GUI hint only. It accepts no payload, commands,
display receipts or settings; display control still uses the private child.
"""
from pathlib import Path

from .instance import InstanceLock, InstanceBusy


class WindowActivation:
    def __init__(self, root: Path) -> None:
        self.lock = InstanceLock(root / 'screen-ui')
        self.request = root / 'screen-ui-show'
        self.owner = False
        self._seen: int | None = None

    def _stamp(self) -> int | None:
        try:
            return self.request.stat().st_mtime_ns
        except FileNotFoundError:
            return None

    def acquire(self, *, background: bool = False) -> bool:
        if self.owner:
            raise RuntimeError('native UI activation already acquired')
        try:
            self.lock.__enter__()
        except InstanceBusy:
            if not background:
                self.request.touch()
            return False
        self.owner = True
        try:
            self._seen = self._stamp()
        except BaseException:
            self.close()
            raise
        return True

    def poll(self) -> bool:
        if not self.owner:
            return False
        current = self._stamp()
        changed = current is not None and current != self._seen
        self._seen = current
        return changed

    def close(self) -> None:
        if self.owner:
            self.owner = False
            self.lock.__exit__(None, None, None)
