"""Exception boundary for the Linux GLib host callbacks."""

import functools
import logging
from typing import Any, Callable

LOG = logging.getLogger(__name__)


def guard(callback: Callable[..., Any], default: Any = True) -> Callable[..., Any]:
    """Wrap a GLib callback so one exception never removes the event source.

    ``default`` is returned after logging the exception; GLib treats a truthy
    return value from a timeout callback as ``SOURCE_CONTINUE``.
    """

    @functools.wraps(callback)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return callback(*args, **kwargs)
        except Exception:
            LOG.exception(
                "unhandled exception in %s", getattr(callback, "__name__", repr(callback))
            )
            return default

    return wrapper
