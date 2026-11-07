"""Linux wire, device and installation constants."""

from __future__ import annotations

from enum import StrEnum

NAME = "work.mmoe.panasonic.Posture"
PATH = "/work/mmoe/panasonic/Posture"
IFACE = NAME
#: One adapter per Linux UID; ownership is verified on the system bus.
SESSION_NAME_PREFIX = NAME + ".Session."


def session_name(uid: int) -> str:
    """Return the system-bus name owned by the session adapter of ``uid``."""
    return f"{SESSION_NAME_PREFIX}u{int(uid)}"


POLKIT_ACTION = NAME + ".configure"
DBUS_ERROR = NAME + ".Error"
RELAY_PROTOCOL = 4
PROFILE_VERSION = 1
SENSOR_SOURCES = ("starting", "iio", "sysfs")

AUTH_CACHE_SECONDS = 2.0
RELAY_HEARTBEAT_SECONDS = 0.5
RELAY_REPORT_TIMEOUT_SECONDS = 2.5
RELAY_LEASE_SECONDS = 3.0
IIO_NO_SAMPLE_SECONDS = 1.0
SENSOR_RETRY_SECONDS = 60.0
DATA_DIR = "/usr/share/panasonic-posture-manager"
CONFIG_DIR = "/etc/panasonic-posture-manager"
LOCALE_DIR = "/usr/share/locale"
STATE_DIR = "/var/lib/panasonic-posture-manager"
SESSION_STATE_DIR_NAME = "panasonic-posture-manager"
LOCALE_DOMAIN = "panasonic-posture-manager"
RELAY_INSTALL_PATH = "/usr/lib/panasonic-posture-manager/relay"
#: Discovery must exclude the relay output to avoid observing its own signal.
RELAY_VIRTUAL_NAME = "Panasonic posture managed switch"
RELAY_VIRTUAL_PHYS = "panasonic-posture-manager/relay"
DBUS_XML = """<node>
  <interface name="work.mmoe.panasonic.Posture">
    <method name="GetState"><arg type="s" direction="out"/></method>
    <method name="SetMode"><arg type="s" direction="in"/></method>
    <method name="Retry"/>
    <method name="SetSuspendTouchWake"><arg type="b" direction="in"/></method>
    <method name="SetTentEnabled"><arg type="b" direction="in"/></method>
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
    <method name="SessionUpdateUnattended">
      <arg type="u" direction="in"/>
      <arg type="b" direction="in"/>
      <arg type="s" direction="in"/>
      <arg type="s" direction="in"/>
    </method>
    <method name="SessionWake">
      <arg type="u" direction="in"/>
      <arg type="t" direction="in"/>
    </method>
    <signal name="StateChanged"><arg type="s"/></signal>
  </interface>
</node>"""


class SensorSource(StrEnum):
    STARTING = "starting"
    IIO = "iio"
    SYSFS = "sysfs"
