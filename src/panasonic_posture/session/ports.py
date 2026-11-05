"""Platform services used by the interactive session runtime."""

from __future__ import annotations

from typing import Protocol

from ..model import StateSnapshot, Target


class SessionClient(Protocol):
    def get_state(self) -> StateSnapshot: ...

    def session_update(
        self,
        generation: int,
        ready: bool,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None: ...

    def confirm_tent(self, epoch: int) -> None: ...

    def confirm_stand(self, epoch: int) -> None: ...


class DisplayBackend(Protocol):
    """Apply a display target and restore any outstanding ownership."""

    def apply(self, target: Target) -> None: ...

    def hold_orientation(self) -> None: ...

    def restore(self, normal: bool = False) -> None: ...


class PostureNotifier(Protocol):
    @property
    def notification(self) -> int | None: ...

    @property
    def epoch(self) -> int | None: ...

    def sync(self, epoch: int | None, *, prompt: str, allow_retry: bool, now: float) -> None: ...

    def close(self) -> None: ...


class DisplayJournal(Protocol):
    """Persist ownership before display writes and support restart recovery."""

    def load(self) -> dict | None: ...

    def save(self, document: dict) -> None: ...

    def clear(self) -> None: ...
