"""Wire protocol constants shared by the system service, session adapter and UI.

Everything in this module is part of the project's *internal* contract.  The
D-Bus bus name/object path/interface/method names are kept stable for
compatibility with the 0.2.x series; the payload schema and the session
adapter identity were revised in 0.3.0.
"""

from __future__ import annotations

# --- D-Bus endpoints ---------------------------------------------------------

NAME = "work.mmoe.panasonic.Posture"
PATH = "/work/mmoe/panasonic/Posture"
IFACE = NAME

# Well-known name prefix owned by the unprivileged session adapter on the
# *system* bus.  The name is per-uid (``...Posture.Session.u<uid>``) so several
# logged-in users can run their own adapter; the daemon verifies that the
# sender owns the name for its uid *and* that the uid's session is the active
# local graphical session before accepting SessionUpdate.  The per-uid name
# also enforces a single adapter instance per user.
#
# D-Bus well-known names may not have an element starting with a digit, hence
# the ``u`` prefix before the numeric uid.
SESSION_NAME_PREFIX = NAME + ".Session."


def session_name(uid: int) -> str:
    """Return the system-bus name owned by the session adapter of ``uid``."""
    return f"{SESSION_NAME_PREFIX}u{int(uid)}"


POLKIT_ACTION = NAME + ".configure"
DBUS_ERROR = NAME + ".Error"

# --- Payload / artifact versions --------------------------------------------

STATE_VERSION = 2
RELAY_PROTOCOL = 2
PROFILE_VERSION = 1

# --- Enumerations (mirrored in ui/errors.js where user visible) -------------

MODES = ("auto", "laptop", "tent", "stand", "tablet")
TARGET_MODES = ("laptop", "tent", "stand", "tablet", "hardware", "unavailable")
POSES = ("laptop", "tent", "stand", "tablet", "unknown")
PROMPTS = ("tent", "stand")
HARDWARE_STATUSES = ("ok", "absent", "ambiguous", "permission", "read_error")
SENSOR_SOURCES = ("starting", "iio", "sysfs")
BACKENDS = ("ready", "error", "development", "unsupported")
SUPPORT_TIERS = ("validated", "compatible", "unsupported")

# --- Timing constants (seconds) ---------------------------------------------

TICK_SECONDS = 0.1
LEASE_SECONDS = 3.0
SESSION_CHECK_SECONDS = 1.0
AUTH_CACHE_SECONDS = 2.0
AGENT_HEARTBEAT_SECONDS = 0.5
AGENT_FAILURE_GRACE_SECONDS = 3.0
RELAY_HEARTBEAT_SECONDS = 0.5
RELAY_REPORT_TIMEOUT_SECONDS = 2.5
RELAY_LEASE_SECONDS = 3.0
#: Detect a stalled IIO stream well before the policy's stale window.  The
#: sysfs fallback itself can take seconds on this sensor hub; waiting the full
#: stale window guarantees an unnecessary software-takeover release.
IIO_NO_SAMPLE_SECONDS = 1.0
SENSOR_RETRY_SECONDS = 60.0
HARDWARE_UNKNOWN_DEBOUNCE_SECONDS = 0.5
DISPLAY_RETRY_BASE_SECONDS = 2.0
DISPLAY_RETRY_MAX_SECONDS = 10.0
SLOW_TICK_WARN_SECONDS = 0.25

# --- Installed data locations ------------------------------------------------

DATA_DIR = "/usr/share/panasonic-posture-manager"
CONFIG_DIR = "/etc/panasonic-posture-manager"
LOCALE_DIR = "/usr/share/locale"
STATE_DIR = "/var/lib/panasonic-posture-manager"
SESSION_STATE_DIR_NAME = "panasonic-posture-manager"
LOCALE_DOMAIN = "panasonic-posture-manager"
RELAY_INSTALL_PATH = "/usr/lib/panasonic-posture-manager/relay"

# Identity of the uinput switch created by the privileged relay.  Both the C
# relay (backend/uinput.c) and the Python discovery code (hardware/sysfs.py)
# must exclude this device from hardware discovery, otherwise the program
# observes its own output on generic profiles.
RELAY_VIRTUAL_NAME = "Panasonic posture managed switch"
RELAY_VIRTUAL_PHYS = "panasonic-posture-manager/relay"

# --- Error codes -------------------------------------------------------------
#
# The daemon reports a stable machine-readable code plus an optional detail;
# the session adapter/UI translate the code.  Never put English prose in a
# user visible field.


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
    """Build the ``<code>: <detail>`` string used as a D-Bus error message."""
    code = str(code or ErrorCode.INTERNAL)
    if detail:
        return f"{code}: {detail}"
    return code


def split_error(message: str) -> tuple[str, str | None]:
    """Split a D-Bus error message back into ``(code, detail)``."""
    text = (message or "").strip()
    code, separator, detail = text.partition(": ")
    if code in ALL_ERROR_CODES:
        return code, detail or None
    return ErrorCode.INTERNAL, text or None


# --- D-Bus introspection XML -------------------------------------------------
#
# SessionUpdate gained an error code and detail in 0.3.0: (u b s s).

DBUS_XML = """<node>
  <interface name="work.mmoe.panasonic.Posture">
    <method name="GetState"><arg type="s" direction="out"/></method>
    <method name="SetMode"><arg type="s" direction="in"/></method>
    <method name="Retry"/>
    <method name="SetTentPrompt"><arg type="b" direction="in"/></method>
    <method name="SetStandPrompt"><arg type="b" direction="in"/></method>
    <method name="CalibrateSensor"/>
    <method name="ResetSensorCalibration"/>
    <method name="ConfirmTent"><arg type="u" direction="in"/></method>
    <method name="ConfirmStand"><arg type="u" direction="in"/></method>
    <method name="SessionUpdate">
      <arg type="u" direction="in"/>
      <arg type="b" direction="in"/>
      <arg type="s" direction="in"/>
      <arg type="s" direction="in"/>
    </method>
    <signal name="StateChanged"><arg type="s"/></signal>
  </interface>
</node>"""
