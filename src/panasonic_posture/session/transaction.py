"""Shared ownership journal and transaction logic for display backends.

Every desktop backend implements only how to read and write the built-in
output; this module owns journaling, read-back, rollback, crash recovery and
the software-mode ownership lifecycle.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from ..model import Target
from .failures import DisplayError, ErrorCode
from .journal_schema import JOURNAL_VERSION
from .ports import DisplayJournal

log = logging.getLogger(__name__)

#: Software modes own the display transaction; unavailable gives it back.
SOFTWARE_MODES = frozenset({"laptop", "tent", "stand", "tablet"})
RESTORE_MODES = frozenset({"hardware", "unavailable"})
HARDWARE_MODE = "hardware"


def desired_transform(mode: str) -> int:
    """Return the landscape transform owned by a software posture mode.

    Tablet mode starts in landscape before releasing the rotation lock, which
    avoids inheriting the 180 degree tent transform when switching modes.
    """

    return 2 if mode == "tent" else 0


@dataclass(frozen=True)
class DisplayState:
    """A backend snapshot with a backend-neutral rotation representation.

    ``transform`` uses the Mutter numbering (0=normal, 1=90, 2=180, 3=270)
    because it is already the journal format used by GNOME.  ``lock`` is a
    bool for on/off backends or the backend's native policy string; it is
    ``None`` when the backend cannot control automatic rotation.
    """

    identity: tuple[str, ...]
    transform: int
    lock: bool | str | None = None
    lock_supported: bool = False


class SoftwareDisplay:
    """Backend-neutral display transaction with crash recovery."""

    backend_name = ""
    lock_is_global = False

    def __init__(
        self,
        journal: DisplayJournal,
        gettext: Callable[[str], str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._ = gettext or (lambda message: message)
        self._sleep = sleep
        self.journal = journal
        self.owned: dict | None = None
        self._recover()

    # -- backend contract -------------------------------------------------

    def _read_state(self) -> DisplayState:
        raise NotImplementedError

    def _read_restore_state(self, identity: tuple[str, ...]) -> DisplayState:
        return self._read_state()

    def _read_global_lock(self) -> bool | str:
        raise NotImplementedError

    def _write_rotation(self, state: DisplayState, transform: int) -> None:
        raise NotImplementedError

    def _write_lock(self, state: DisplayState, locked: bool) -> None:
        raise NotImplementedError

    def _lock_value(self, locked: bool) -> bool | str:
        return locked

    def _write_original_lock(self, state: DisplayState, policy: str) -> None:
        raise NotImplementedError

    def _validate(self, state: DisplayState, mode: str, transform: int) -> None:
        """Reject unsupported layouts before the ownership journal is written."""

        del state, mode, transform

    # -- public API -------------------------------------------------------

    def apply(self, target: Target | dict) -> None:
        """Apply *target*; raises :class:`DisplayError` on failure."""

        if isinstance(target, dict):
            raw_mode = target.get("mode")
            if raw_mode not in SOFTWARE_MODES | RESTORE_MODES:
                raise DisplayError(
                    ErrorCode.DISPLAY_UNKNOWN_MODE, str(raw_mode or "empty")
                )
            try:
                target = Target.from_dict(target)
            except (TypeError, ValueError) as error:
                raise DisplayError(
                    ErrorCode.INTERNAL, "invalid-display-target"
                ) from error
        mode = str(target.mode or "")
        if mode == HARDWARE_MODE:
            self._apply_hardware()
            return
        if mode in RESTORE_MODES:
            self.restore()
            return
        if mode not in SOFTWARE_MODES:
            raise DisplayError(ErrorCode.DISPLAY_UNKNOWN_MODE, mode or "empty")
        state = self._read_transaction_state()
        transform = desired_transform(mode)
        self._validate(state, mode, transform)
        if not self.owned:
            self.owned = self._journal_document(state)
            self._persist()
        try:
            if state.lock_supported:
                self._write_owned_lock(state, True)
            # Native tablet rotation can finish while the lock is being set.
            # Even a seemingly redundant target must use a post-lock snapshot
            # before claiming its direction as successfully applied.
            current = self._read_state()
            if current.identity != state.identity:
                raise DisplayError(
                    ErrorCode.DISPLAY_IDENTITY, "internal-monitor-identity-changed"
                )
            state = current
            if state.transform != transform:
                # Until the write is verified, recovery must recognize both
                # the last owned transform and the new write intent.
                self.owned["pending_transform"] = transform
                self._persist()
                self._write_rotation(state, transform)
                self.owned.pop("pending_transform")
            self.owned["written_transform"] = transform
            self._persist()
            if mode == "tablet" and state.lock_supported:
                # Release automatic rotation only after landscape is stable.
                current = self._read_state()
                if current.identity != state.identity:
                    raise DisplayError(
                        ErrorCode.DISPLAY_IDENTITY, "internal-monitor-identity-changed"
                    )
                self._write_owned_lock(current, False)
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def _apply_hardware(self) -> None:
        """Release the panel to native hardware tablet mode.

        Keep the current direction while the desktop takes over rotation.
        Only lock ownership remains, so recovery cannot undo a native rotation
        merely because it happens to match our previous software direction.
        """
        state = self._read_transaction_state()
        if not self.owned:
            self.owned = self._journal_document(state)
            self._persist()
        try:
            # Relinquish rotation before unlocking: a crash after the desktop
            # takes over must not restore a coincidentally matching direction.
            self.owned["written_transform"] = None
            self._persist()
            if state.lock_supported:
                self._write_owned_lock(state, False)
            if not state.lock_supported:
                self.journal.clear()
                self.owned = None
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def hold_orientation(self) -> None:
        """Temporarily lock the current direction without writing a rotation."""
        state = self._read_transaction_state()
        if not state.lock_supported:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "orientation-lock-unavailable")
        if not self.owned:
            self.owned = self._journal_document(state)
            self._persist()
        try:
            self._write_owned_lock(state, True)
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def restore(
        self,
        normal: bool = False,
        *,
        release_lock: bool = False,
        keep_journal: bool = False,
    ) -> None:
        """Undo journaled changes and keep the journal if recovery fails.

        ``release_lock`` actively unlocks automatic rotation instead of
        restoring the captured policy (retained for existing callers).
        ``keep_journal`` leaves the original state available for the eventual
        hardware-to-software transition or session shutdown.
        """

        owned = self.owned
        if not owned:
            return
        identity = tuple(str(part) for part in owned.get("spec") or ())
        owned_transforms = {owned.get("written_transform"), owned.get("pending_transform")}
        owned_transforms.discard(None)
        has_owned_lock = any(
            owned.get(field) is not None for field in ("written_lock", "pending_lock")
        )
        restore_global_lock = self.lock_is_global and has_owned_lock and not release_lock
        state = self._read_for_restore(identity, release_lock, restore_global_lock)
        failure: DisplayError | None = None

        if owned_transforms:
            if state.identity == identity and state.transform in owned_transforms:
                original = 0 if normal else int(owned.get("original_transform") or 0)
                if state.transform != original:
                    try:
                        self._write_rotation(state, original)
                    except DisplayError as error:
                        failure = error
                    if release_lock or has_owned_lock:
                        # A lock edit may have arrived during compositor read-back.
                        state = self._read_for_restore(identity, release_lock, restore_global_lock)
            elif state.identity == identity:
                log.debug("display rotation already %s; not restoring", state.transform)
            else:
                log.warning("display output changed; leaving rotation untouched")

        if release_lock and state.identity != identity:
            raise DisplayError(
                ErrorCode.DISPLAY_IDENTITY, "internal-monitor-identity-changed"
            )
        # A global lock can still be restored when the original panel is gone.
        if state.identity == identity or (self.lock_is_global and state.lock is not None):
            try:
                if release_lock and (keep_journal or has_owned_lock):
                    self._write_owned_lock(state, False)
                elif not release_lock and has_owned_lock:
                    self._restore_lock(state)
                    if failure is not None and restore_global_lock:
                        self._forget_restored_lock()
            except DisplayError as error:
                failure = failure or error

        if failure is not None:
            raise failure
        if keep_journal:
            return
        self.journal.clear()
        self.owned = None

    def _read_for_restore(
        self, identity: tuple[str, ...], release_lock: bool, restore_global_lock: bool
    ) -> DisplayState:
        try:
            return self._read_state() if release_lock else self._read_restore_state(identity)
        except DisplayError:
            if restore_global_lock:
                # An inactive or mirrored panel can block rotation recovery
                # without preventing the independent desktop setting write.
                self._restore_lock(DisplayState((), 0, self._read_global_lock(), True))
                self._forget_restored_lock()
            raise

    def _forget_restored_lock(self) -> None:
        # Retain the outstanding rotation, but do not undo a later user lock
        # change when the panel becomes usable and rotation recovery retries.
        document = self.owned.copy()
        document["written_lock"] = None
        document.pop("pending_lock", None)
        self.journal.save(document)
        self.owned = document

    # -- lock ownership ---------------------------------------------------

    def _owns_lock(self, state: DisplayState) -> bool:
        return any(
            isinstance(value, bool) and state.lock == self._lock_value(value)
            for value in (self.owned.get("written_lock"), self.owned.get("pending_lock"))
        )

    def _write_owned_lock(self, state: DisplayState, locked: bool) -> None:
        if not state.lock_supported:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "orientation-lock-unavailable")
        if state.lock == self._lock_value(locked):
            return
        if not self._owns_lock(state):
            # A fresh write takes ownership of the current desktop preference.
            self.owned["original_lock"] = self._is_locked(state)
            if isinstance(state.lock, str):
                self.owned["original_policy"] = state.lock
            self.owned["written_lock"] = None
        self.owned["pending_lock"] = locked
        self._persist()
        self._write_lock(state, locked)
        self.owned["written_lock"] = locked
        self.owned.pop("pending_lock")
        self._persist()

    def _restore_lock(self, state: DisplayState) -> None:
        if not state.lock_supported:
            raise DisplayError(ErrorCode.DISPLAY_LOCK, "orientation-lock-unavailable")
        if not self._owns_lock(state):
            return
        original_policy = self.owned.get("original_policy")
        if original_policy is not None:
            self._write_original_lock(state, str(original_policy))
        else:
            self._write_lock(state, bool(self.owned["original_lock"]))

    # -- journal ----------------------------------------------------------

    def _read_transaction_state(self) -> DisplayState:
        """Resolve an interrupted display write before starting a new transaction."""

        state = self._read_state()
        if not self.owned:
            return state
        output_changed = tuple(self.owned["spec"]) != state.identity
        if output_changed:
            # Never project an old output's original transform or lock policy
            # onto a replacement panel.  Restore what can be restored, then let
            # the caller capture a fresh transaction for the current output.
            log.warning("internal monitor changed; restarting the display transaction")
        if output_changed or any(
            self.owned.get(field) is not None for field in ("pending_transform", "pending_lock")
        ):
            self.restore()
            state = self._read_state()
        return state

    def _journal_document(self, state: DisplayState) -> dict:
        document = {
            "version": JOURNAL_VERSION,
            "backend": self.backend_name,
            "spec": [str(part) for part in state.identity],
            "original_transform": int(state.transform),
            "original_lock": state.lock is None or self._is_locked(state),
            "written_lock": None,
            "written_transform": None,
        }
        if isinstance(state.lock, str):
            document["original_policy"] = state.lock
        return document

    def _persist(self) -> None:
        self.journal.save(self.owned)  # type: ignore[arg-type]

    def _rollback(self) -> None:
        try:
            self.restore()
        except (DisplayError, OSError, ValueError) as error:
            log.warning("display rollback failed; journal kept: %s", error)

    def _recover(self) -> None:
        document = self.journal.load()
        if not document:
            return
        backend = document.get("backend", "gnome")
        if backend != self.backend_name:
            log.warning(
                "display journal belongs to the %s backend, not %s; ignoring it",
                backend,
                self.backend_name,
            )
            return
        self.owned = document
        try:
            self.restore()
        except (DisplayError, OSError, ValueError) as error:
            # The compositor may not be ready during early session startup;
            # keep the journal so the next transaction or start retries.
            log.warning("display recovery pending: %s", error)

    def _is_locked(self, state: DisplayState) -> bool:
        return state.lock == self._lock_value(True)


__all__ = [
    "DisplayState",
    "RESTORE_MODES",
    "SOFTWARE_MODES",
    "SoftwareDisplay",
    "desired_transform",
]
