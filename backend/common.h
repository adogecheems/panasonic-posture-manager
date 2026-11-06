/*
 * Shared definitions for the privileged relay (backend/).
 *
 * The relay is the only root component that touches /dev/uinput and
 * /dev/input.  It never reads keyboard event content: it queries device
 * capabilities and switch state, and optionally grabs the internal keyboard
 * and touchpad while the aggregated tablet mode is on. It may observe contact
 * on a unique touchscreen and emit a session-approved KEY_WAKEUP (no text).
 */
#ifndef PPM_COMMON_H
#define PPM_COMMON_H

#include <errno.h>
#include <fcntl.h>
#include <linux/input.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

/*
 * Identity of the uinput switch created by the relay.  Every discovery path
 * (switch and input alike) must reject devices carrying this name or phys:
 * the program must never observe its own output as hardware.
 */
#define VIRTUAL_NAME "Panasonic posture managed switch"
#define VIRTUAL_PHYS "panasonic-posture-manager/relay"
#define WAKE_NAME "Panasonic posture touch wake"
#define WAKE_PHYS "panasonic-posture-manager/wake"

/* stdout protocol version written to the service, one JSON object per line. */
#define RELAY_PROTOCOL 4
#define RELAY_TOUCH_SECONDS 2.0

/* Lease and timing policy (seconds / milliseconds). */
#define RELAY_LEASE_SECONDS 3.0
#define RELAY_HANDOFF_SECONDS 2.0
#define RELAY_POLL_MILLISECONDS 100
#define RELAY_REPORT_SECONDS 1.0
#define RELAY_SCAN_SECONDS 1.0
#define RELAY_SWITCH_RETRY_LIMIT 5
#define RELAY_ATTACH_RETRY_MILLISECONDS 200
#define RELAY_INPUT_RETRY_SECONDS 1.0
#define RELAY_INPUT_RECHECK_SECONDS 2.0
#define RELAY_EVENT_DELAY_MILLISECONDS 150
/* Delay between the two recovery events (known state, then final state). */
#define RELAY_RECOVER_TRANSITION_MILLISECONDS 100

/* Exit codes; mirrored by the service's error mapping. */
enum relay_exit_code {
    RELAY_EXIT_OK = 0,
    RELAY_EXIT_USAGE = 2,     /* bad arguments or not running as root */
    RELAY_EXIT_HARDWARE = 3,  /* physical switch unreadable or ambiguous */
    RELAY_EXIT_UINPUT = 4,    /* cannot create the virtual switch */
    RELAY_EXIT_SWITCH = 5,    /* switch present but cannot be grabbed */
    RELAY_EXIT_EMIT = 8,      /* cannot write an input event */
    RELAY_EXIT_POLL = 9,      /* unexpected poll failure */
    RELAY_EXIT_PROTOCOL = 10, /* invalid byte on stdin */
};

/*
 * stderr logging.  The relay never prints logs to stdout: that stream is the
 * machine-readable protocol consumed by the service.
 */
__attribute__((format(printf, 1, 2))) static inline void relay_log(
        const char *format, ...) {
    va_list arguments;

    fputs("panasonic-posture relay: ", stderr);
    va_start(arguments, format);
    vfprintf(stderr, format, arguments);
    va_end(arguments);
    fputc('\n', stderr);
}

static inline void relay_log_errno(const char *what) {
    fprintf(stderr, "panasonic-posture relay: %s: %s\n", what, strerror(errno));
}

/*
 * Open a character device with the hardening used by every discovery path:
 * close-on-exec, no symlink following and a character device check.  This
 * also narrows the glob-to-open TOCTOU window.
 */
static inline int relay_open_character_device(const char *path, int flags) {
    struct stat status;
    int descriptor = open(path, flags | O_CLOEXEC | O_NOFOLLOW);

    if (descriptor < 0)
        return -1;
    if (fstat(descriptor, &status) < 0 || !S_ISCHR(status.st_mode)) {
        close(descriptor);
        return -1;
    }
    return descriptor;
}

static inline int relay_open_event_device(const char *path) {
    return relay_open_character_device(path, O_RDONLY | O_NONBLOCK);
}

/*
 * Read a small sysfs text attribute and strip the trailing newline.  Returns
 * 0 on success, -1 on any error.
 */
static inline int relay_read_sysfs_text(const char *path, char *buffer,
                                        size_t size) {
    ssize_t length;
    int descriptor;

    if (size < 2)
        return -1;
    descriptor = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0)
        return -1;
    length = read(descriptor, buffer, size - 1);
    close(descriptor);
    if (length <= 0)
        return -1;
    buffer[length] = '\0';
    while (length > 0 &&
           (buffer[length - 1] == '\n' || buffer[length - 1] == '\r')) {
        length--;
        buffer[length] = '\0';
    }
    return 0;
}

/* Parse "/dev/input/eventN" into N.  Returns 0 on success, -1 otherwise. */
static inline int relay_event_index(const char *path, int *index) {
    const char *base;
    char *end = NULL;
    long value;

    if (path == NULL)
        return -1;
    base = strrchr(path, '/');
    base = base != NULL ? base + 1 : path;
    if (strncmp(base, "event", 5) != 0)
        return -1;
    errno = 0;
    value = strtol(base + 5, &end, 10);
    if (errno != 0 || end == base + 5 || end == NULL || *end != '\0')
        return -1;
    if (value < 0 || value > 1000000)
        return -1;
    if (index != NULL)
        *index = (int)value;
    return 0;
}

/*
 * Cross-check an opened event fd against the sysfs entry for the same event
 * number.  This catches a device node that was recycled between glob() and
 * open() (TOCTOU).  Returns 1 when the identity checks out or cannot be
 * checked, 0 when the device changed.
 */
static inline int relay_identity_matches_sysfs(int fd, const char *device_path) {
    char path[128];
    char expected[256];
    char actual[256];
    int index = -1;
    int length;

    if (relay_event_index(device_path, &index) < 0)
        return 1; /* non-standard node name: nothing to cross-check */

    length = snprintf(path, sizeof path, "/sys/class/input/event%d/device/name",
                      index);
    if (length < 0 || (size_t)length >= sizeof path)
        return 1;
    if (relay_read_sysfs_text(path, expected, sizeof expected) < 0)
        return 0; /* the device vanished between glob() and open() */

    memset(actual, 0, sizeof actual);
    if (ioctl(fd, EVIOCGNAME(sizeof actual), actual) < 0)
        return 0;
    actual[sizeof actual - 1] = '\0';
    if (strcmp(expected, actual) != 0)
        return 0;

    length = snprintf(path, sizeof path, "/sys/class/input/event%d/device/phys",
                      index);
    if (length < 0 || (size_t)length >= sizeof path)
        return 1;
    if (relay_read_sysfs_text(path, expected, sizeof expected) == 0) {
        memset(actual, 0, sizeof actual);
        if (ioctl(fd, EVIOCGPHYS(sizeof actual), actual) == 0) {
            actual[sizeof actual - 1] = '\0';
            if (strcmp(expected, actual) != 0)
                return 0;
        }
    }
    return 1;
}

#endif /* PPM_COMMON_H */
