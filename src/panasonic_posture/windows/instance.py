"""One software host per settings file; OS locks are released after a crash."""
from pathlib import Path
import os


class InstanceLock:
    def __init__(self, settings: Path):
        self.path = settings.with_name(settings.name + '.lock')
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        self.stream = os.fdopen(descriptor, 'r+b', buffering=0)
        try:
            if os.name == 'nt':
                import msvcrt
                if not self.stream.read(1):
                    self.stream.write(b'\0')
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __exit__(self, *args):
        # Closing the handle releases the lock on both platforms. Keep the
        # lock file so another process cannot create a competing inode.
        self.stream.close()
        self.stream = None
