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
from . import errors
from .errors import DisplayError, ErrorCode
from .journal import JOURNAL_VERSION, Journal, default_path

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
    bool for on/off backends or the native policy string for Plasma; it is
    ``None`` when the backend cannot control automatic rotation.  ``auxiliary``
    is backend-private, JSON-serializable state that must be restored with the
    rotation (X11 uses it for touchscreen coordinate matrices).
    """

    identity: tuple[str, ...]
    transform: int
    lock: bool | str | None = None
    lock_supported: bool = False
    auxiliary: dict | None = None


class SoftwareDisplay:
    """Backend-neutral display transaction with crash recovery."""

    backend_name = ""

    def __init__(
        self,
        journal: Journal | None = None,
        gettext: Callable[[str], str] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._ = gettext or errors.get_translator()
        self._sleep = sleep
        self.journal = journal if journal is not None else Journal(default_path())
        self.owned: dict | None = None
        self._recover()

    # -- backend contract -------------------------------------------------

    def _read_state(self) -> DisplayState:
        raise NotImplementedError

    def _write_rotation(self, state: DisplayState, transform: int) -> None:
        raise NotImplementedError

    def _write_lock(self, state: DisplayState, locked: bool) -> None:
        raise NotImplementedError

    def _write_original_lock(self, state: DisplayState, policy: str) -> None:
        raise NotImplementedError

    def _validate(self, state: DisplayState, mode: str, transform: int) -> None:
        """Reject unsupported layouts before the ownership journal is written."""

        del state, mode, transform

    def _write_auxiliary(self, state: DisplayState) -> None:
        """Apply backend-private state after the rotation is visible."""

        del state

    def _restore_auxiliary(self, state: DisplayState, original: dict) -> None:
        """Restore backend-private state captured in the journal."""

        del state, original

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
        if mode == "laptop":
            self._apply_laptop()
            return

        state = self._read_transaction_state()
        transform = desired_transform(mode)
        self._validate(state, mode, transform)
        if not self.owned:
            self.owned = self._journal_document(state)
            self._persist()
        try:
            if state.lock_supported:
                self.owned["written_lock"] = True
                self._persist()
                if not self._is_locked(state):
                    self._write_lock(state, True)
            self.owned["written_transform"] = transform
            self._persist()
            if state.transform != transform:
                self._write_rotation(state, transform)
            if state.auxiliary is not None:
                if "original_auxiliary" not in self.owned:
                    # The first capture may have failed while the display
                    # transaction already existed.  Adopt the current state
                    # before mapping so every write remains recoverable.
                    self.owned["original_auxiliary"] = state.auxiliary
                    self.owned["written_auxiliary"] = None
                    self._persist()
                if self.owned.get("written_auxiliary") is not True:
                    self.owned["written_auxiliary"] = True
                    self._persist()
                self._write_auxiliary(state)
            if mode == "tablet" and state.lock_supported:
                # Release automatic rotation only after landscape is stable.
                self.owned["written_lock"] = False
                self._persist()
                self._write_lock(state, False)
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def _apply_laptop(self) -> None:
        """Return to landscape and retain ownership until restore is requested.

        Laptop is a software mode just like tent/stand/tablet: it locks the
        display in landscape but must keep the original transform/policy in
        the journal.  Otherwise a later ``hardware``/``unavailable`` target
        cannot undo the landscape lock (P1).
        """

        state = self._read_transaction_state()
        self._validate(state, "laptop", 0)
        if not self.owned:
            self.owned = self._journal_document(state)
            self._persist()
        try:
            if state.lock_supported:
                self.owned["written_lock"] = True
                self._persist()
                if not self._is_locked(state):
                    self._write_lock(state, True)
            self.owned["written_transform"] = 0
            self._persist()
            if state.transform != 0:
                self._write_rotation(state, 0)
            original_auxiliary = self.owned.get("original_auxiliary")
            if (
                self.owned.get("written_auxiliary") is True
                and isinstance(original_auxiliary, dict)
            ):
                self._restore_auxiliary(state, original_auxiliary)
                # The original matrix is active again.  A later software mode
                # must re-apply it after rotating.
                self.owned["written_auxiliary"] = False
                self._persist()
        except (DisplayError, OSError, ValueError):
            self._rollback()
            raise

    def _apply_hardware(self) -> None:
        """Release the panel to native hardware tablet mode.

        The physical switch means the platform should own rotation.  Restore
        the pre-takeover transform and input mapping, but override a locked
        orientation policy with the unlocked equivalent so automatic rotation
        keeps working in tablet mode.  The journal stays in place so leaving
        hardware mode can restore the user's original lock policy.
        """

        state = self._read_transaction_state()
        if not self.owned:
            self.owned = self._journal_document(state)
            if state.lock_supported:
                # No software mode touched the lock yet, but restore() must
                # still put the captured original policy back later.
                self.owned["written_lock"] = True
            self._persist()
        # Only a controllable orientation policy needs a deferred journal:
        # leaving hardware mode must restore the user's original lock.  X11
        # has no lock policy, so its transform/input transaction is complete
        # once restore() returns and the journal must be cleared as usual.
        self.restore(release_lock=True, keep_journal=state.lock_supported)

    def restore(
        self,
        normal: bool = False,
        *,
        release_lock: bool = False,
        keep_journal: bool = False,
    ) -> None:
        """Undo journaled changes and keep the journal if recovery fails.

        ``release_lock`` is used by hardware tablet mode: instead of restoring
        the captured lock policy, actively unlock automatic rotation.
        ``keep_journal`` leaves the original state available for the eventual
        hardware-to-software transition or session shutdown.
        """

        owned = self.owned
        if not owned:
            return
        state = self._read_state()
        identity = tuple(str(part) for part in owned.get("spec") or ())
        written_transform = owned.get("written_transform")
        failure: DisplayError | None = None

        if written_transform is not None:
            if state.identity == identity and state.transform == written_transform:
                original = 0 if normal else int(owned.get("original_transform") or 0)
                try:
                    self._write_rotation(state, original)
                except DisplayError as error:
                    failure = error
            elif state.identity == identity:
                log.debug("display rotation already %s; not restoring", state.transform)
            else:
                log.warning("display output changed; leaving rotation untouched")

        if (
            owned.get("written_auxiliary") is True
            and isinstance(owned.get("original_auxiliary"), dict)
        ):
            try:
                self._restore_auxiliary(state, owned["original_auxiliary"])
            except DisplayError as error:
                failure = failure or error

        if owned.get("written_lock") is not None:
            try:
                if not state.lock_supported:
                    raise DisplayError(
                        ErrorCode.DISPLAY_LOCK, "orientation-lock-unavailable"
                    )
                if release_lock:
                    self._write_lock(state, False)
                else:
                    original_policy = owned.get("original_policy")
                    if original_policy is not None:
                        self._write_original_lock(state, str(original_policy))
                    else:
                        self._write_lock(state, bool(owned.get("original_lock")))
            except DisplayError as error:
                failure = failure or error

        if failure is not None:
            raise failure
        if keep_journal:
            return
        self.journal.clear()
        self.owned = None

    # -- journal ----------------------------------------------------------

    def _read_transaction_state(self) -> DisplayState:
        """Read state, closing a journal that belongs to another output first."""

        state = self._read_state()
        if self.owned and tuple(
            str(part) for part in self.owned.get("spec") or ()
        ) != state.identity:
            # Never project an old output's original transform or lock policy
            # onto a replacement panel.  Restore what can be restored, then let
            # the caller capture a fresh transaction for the current output.
            log.warning("internal monitor changed; restarting the display transaction")
            self.restore()
            state = self._read_state()
        return state

    def _journal_document(self, state: DisplayState) -> dict:
        if isinstance(state.lock, bool):
            original_lock = state.lock
        elif isinstance(state.lock, str):
            original_lock = state.lock == "never"
        else:
            original_lock = True
        document = {
            "version": JOURNAL_VERSION,
            "backend": self.backend_name,
            "spec": [str(part) for part in state.identity],
            "original_transform": int(state.transform),
            "original_lock": bool(original_lock),
            "written_lock": None,
            "written_transform": None,
        }
        if isinstance(state.lock, str):
            document["original_policy"] = state.lock
        if state.auxiliary is not None:
            document["original_auxiliary"] = state.auxiliary
            document["written_auxiliary"] = None
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

    @staticmethod
    def _is_locked(state: DisplayState) -> bool:
        return state.lock is True or state.lock == "never"


__all__ = [
    "DisplayState",
    "RESTORE_MODES",
    "SOFTWARE_MODES",
    "SoftwareDisplay",
    "desired_transform",
]
