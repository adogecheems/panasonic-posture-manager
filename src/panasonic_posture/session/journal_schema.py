"""Display journal v1 validation; no filesystem access."""

from typing import Any

JOURNAL_VERSION = 1
BACKEND_SPEC_FIELDS = {"gnome": 4, "plasma": 1}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_mode(value: Any) -> bool:
    return (
        isinstance(value, (int, str))
        and not isinstance(value, bool)
        and value != ""
    )


def validate(data: Any) -> dict:
    """Validate a loaded journal document, raising ``ValueError`` if invalid."""

    if not isinstance(data, dict):
        raise ValueError("journal must be a JSON object")
    if data.get("version") != JOURNAL_VERSION:
        raise ValueError(f"unsupported journal version {data.get('version')!r}")
    backend = data.get("backend", "gnome")
    if not isinstance(backend, str) or not backend:
        raise ValueError("journal backend must be a non-empty string")
    expected_fields = BACKEND_SPEC_FIELDS.get(backend, 1)
    spec = data.get("spec")
    if not isinstance(spec, (list, tuple)) or len(spec) != expected_fields:
        raise ValueError(
            f"journal spec for {backend} must be a list of {expected_fields} strings"
        )
    if not all(isinstance(part, str) for part in spec):
        raise ValueError("journal spec must be a list of strings")
    if not _is_int(data.get("original_transform")):
        raise ValueError("journal original_transform must be an integer")
    if not isinstance(data.get("original_lock"), bool):
        raise ValueError("journal original_lock must be a boolean")
    for field in ("written_lock", "pending_lock"):
        lock = data.get(field)
        if lock is not None and not isinstance(lock, bool):
            raise ValueError(f"journal {field} must be a boolean or null")
    for field in ("written_transform", "pending_transform"):
        transform = data.get(field)
        if transform is not None and not _is_int(transform):
            raise ValueError(f"journal {field} must be an integer or null")
    original_policy = data.get("original_policy", None)
    if original_policy is not None and not isinstance(original_policy, str):
        raise ValueError("journal original_policy must be a string or null")
    shell_fields = ("written_shell_mode", "pending_shell_mode")
    has_shell_ownership = any(data.get(field) is not None for field in shell_fields)
    original_shell_mode = data.get("original_shell_mode")
    if original_shell_mode is not None and not _is_mode(original_shell_mode):
        raise ValueError("journal original_shell_mode must be a mode or absent")
    if has_shell_ownership and original_shell_mode is None:
        raise ValueError("journal shell ownership requires original_shell_mode")
    for field in shell_fields:
        mode = data.get(field)
        if mode is not None and not _is_mode(mode):
            raise ValueError(f"journal {field} must be a mode or null")
    return data
