"""Process entry point for the private Windows software backend."""
from .private_backend import main, serve, startup_failure_reason

__all__ = ['main', 'serve', 'startup_failure_reason']


if __name__ == '__main__':
    raise SystemExit(main())
