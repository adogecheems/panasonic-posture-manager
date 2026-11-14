#!/bin/bash
# Bounded root helper for the absent-hardware integration test.
#
# The intel-hid driver only creates its switch input node after the first
# switch event.  To exercise the service's virtual-only relay path without
# unbinding the driver, remove the /dev node for a few seconds and recreate
# the exact same device node afterwards.  The sysfs device and libinput's
# already-open file descriptor are untouched; the relay must notice the
# missing node, keep the software tablet request alive, and reattach when the
# node returns.
#
# Security: the marker directory is created by the unprivileged parent with
# tempfile.mkdtemp() (mode 0700, owned by that user) and passed as argv[1];
# argv[2] is the expected owner uid.  This helper refuses symlinks, foreign
# owners and non-0700 directories, and never changes directory permissions.
#
# Usage: absent_switch_root.sh <marker-dir> <expected-owner-uid>
set -u

marker=${1:-}
expected_uid=${2:-}
if [ -z "$marker" ] || [ -z "$expected_uid" ]; then
    echo "usage: absent_switch_root.sh <marker-dir> <expected-owner-uid>" >&2
    exit 2
fi
if [ ! -d "$marker" ] || [ -L "$marker" ]; then
    echo "ERROR: marker path is not a real directory: $marker" >&2
    exit 2
fi
owner=$(stat -c '%u' -- "$marker") || exit 2
mode=$(stat -c '%a' -- "$marker") || exit 2
if [ "$owner" != "$expected_uid" ] || [ "$mode" != "700" ]; then
    echo "ERROR: refusing marker directory owner=$owner mode=$mode" >&2
    exit 2
fi

umask 022
mark() {
    : > "$marker/$1" || exit 1
}

restore_node() {
    [ -n "${node_name:-}" ] || return 0
    if [ ! -e "/dev/input/$node_name" ]; then
        mknod "/dev/input/$node_name" c "$major" "$minor" || return 1
        chown root:input "/dev/input/$node_name" || return 1
        chmod 0660 "/dev/input/$node_name" || return 1
    fi
    if command -v udevadm >/dev/null 2>&1; then
        udevadm trigger --sysname-match="$node_name" --action=change || true
    fi
}

node_name=
major=
minor=
trap 'restore_node' EXIT
trap 'exit 0' INT TERM
mark ready
# Leave time for the caller to select tent mode after the package install
# restarted the system service and the session adapter reconnected.
sleep 10

found=
for device in /sys/class/input/event*; do
    [ -e "$device" ] || continue
    if [ "$(cat "$device/device/name" 2>/dev/null)" = "Intel HID switches" ]; then
        found=$device
        break
    fi
done

if [ -z "$found" ]; then
    echo "SKIP: the hardware switch is already absent" >&2
    mark skipped
    exit 0
fi

node_name=$(basename "$found")
dev=$(cat "$found/dev")
major=${dev%%:*}
minor=${dev##*:}
if [ ! -e "/dev/input/$node_name" ]; then
    echo "SKIP: /dev/input/$node_name is missing" >&2
    mark skipped
    exit 0
fi

echo "Removing /dev/input/$node_name ($dev) for the virtual-only test" >&2
rm -f "/dev/input/$node_name" || {
    echo "ERROR: cannot remove /dev/input/$node_name" >&2
    mark removed_failed
    exit 1
}
mark removed
sleep 15
restore_node || {
    echo "ERROR: cannot restore /dev/input/$node_name" >&2
    mark restore_failed
    exit 1
}
trap - EXIT INT TERM
mark restored
echo "Restored /dev/input/$node_name" >&2
