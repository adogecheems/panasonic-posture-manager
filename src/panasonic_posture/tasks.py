"""Small executor helpers for moving bounded blocking work off GLib ticks."""

from __future__ import annotations

from concurrent.futures import Executor, Future, ThreadPoolExecutor
from typing import Any, Callable


class InlineExecutor(Executor):
    """Deterministic executor used by unit tests and synchronous callers."""

    def submit(
        self,
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        del wait, cancel_futures


def thread_executor(prefix: str) -> ThreadPoolExecutor:
    """Return a single-worker executor for one serialized blocking pipeline."""

    return ThreadPoolExecutor(max_workers=1, thread_name_prefix=prefix)


__all__ = ["InlineExecutor", "thread_executor"]
