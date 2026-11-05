"""Platform-independent domain constants and stable error codes."""

from __future__ import annotations

STATE_VERSION = 2
MODES = ("auto", "laptop", "tent", "stand", "tablet")
TARGET_MODES = ("laptop", "tent", "stand", "tablet", "hardware", "unavailable")
POSES = ("laptop", "tent", "stand", "tablet", "unknown")
PROMPTS = ("tent", "stand")
HARDWARE_STATUSES = ("ok", "absent", "ambiguous", "permission", "read_error")
BACKENDS = ("ready", "error", "development", "unsupported")
SUPPORT_TIERS = ("validated", "compatible", "unsupported")
TICK_SECONDS = 0.1
LEASE_SECONDS = 3.0
SESSION_CHECK_SECONDS = 1.0
AGENT_HEARTBEAT_SECONDS = 0.5
AGENT_FAILURE_GRACE_SECONDS = 3.0
HARDWARE_UNKNOWN_DEBOUNCE_SECONDS = 0.5
DISPLAY_RETRY_BASE_SECONDS = 2.0
DISPLAY_RETRY_MAX_SECONDS = 10.0
SLOW_TICK_WARN_SECONDS = 0.25


class ErrorCode:
    # method / model errors
    INVALID_MODE = "invalid-mode"
    ACCESS_DENIED = "access-denied"
    UNSUPPORTED_MODEL = "unsupported-model"
    HARDWARE_PRIORITY = "hardware-priority"
    EXPIRED_CONFIRMATION = "expired-confirmation"
    CONFIG_WRITE = "config-write-failed"
    # privileged executor errors
    RELAY_START = "relay-start-failed"
    RELAY_EXITED = "relay-exited"
    RELAY_HEARTBEAT = "relay-heartbeat-timeout"
    RELAY_INPUT = "relay-input-grab-failed"
    NATIVE_RECOVERY = "native-state-recovery-failed"
    # sensor errors
    SENSOR_TIMEOUT = "sensor-timeout"
    CALIBRATION_NOT_READY = "calibration-not-ready"
    # display / session errors
    DISPLAY_MIRRORED = "display-mirrored-or-disabled"
    DISPLAY_LAYOUT = "display-layout-conflict"
    DISPLAY_TRANSFORM = "display-transform-not-applied"
    DISPLAY_LOCK = "display-rotation-lock-failed"
    DISPLAY_UNAVAILABLE = "display-unavailable"
    DISPLAY_UNKNOWN_MODE = "display-unknown-mode"
    DISPLAY_IDENTITY = "display-internal-monitor-changed"
    # fallback
    INTERNAL = "internal-error"


ALL_ERROR_CODES = frozenset(
    value
    for name, value in vars(ErrorCode).items()
    if not name.startswith("_") and isinstance(value, str)
)


def error_message(code: str, detail: str | None = None) -> str:
    """Build the ``<code>: <detail>`` string used for stable error diagnostics."""
    code = str(code or ErrorCode.INTERNAL)
    if detail:
        return f"{code}: {detail}"
    return code


def split_error(message: str) -> tuple[str, str | None]:
    """Split a stable error message back into ``(code, detail)``."""
    text = (message or "").strip()
    code, separator, detail = text.partition(": ")
    if code in ALL_ERROR_CODES:
        return code, detail or None
    return ErrorCode.INTERNAL, text or None
