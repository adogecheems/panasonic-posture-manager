/*
 * The relay's virtual SW_TABLET_MODE switch.
 *
 * Exactly one switch-only uinput device is exposed to GNOME while the relay
 * runs.  Its fixed name and phys are the identity used by all discovery code
 * to exclude it from hardware detection.
 */
#include "uinput.h"

#include <linux/input.h>
#include <linux/uinput.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include "common.h"

#ifndef SW_TABLET_MODE
#define SW_TABLET_MODE 0x01
#endif

int uinput_is_own(const char *name, const char *phys) {
    if (name != NULL && (strcmp(name, VIRTUAL_NAME) == 0 || strcmp(name, WAKE_NAME) == 0))
        return 1;
    if (phys != NULL && (strcmp(phys, VIRTUAL_PHYS) == 0 || strcmp(phys, WAKE_PHYS) == 0))
        return 1;
    return 0;
}

int uinput_create_wake(void) {
    struct uinput_setup setup;
    int descriptor = relay_open_character_device("/dev/uinput", O_WRONLY | O_NONBLOCK);

    if (descriptor < 0)
        return -1;
    memset(&setup, 0, sizeof setup);
    snprintf(setup.name, sizeof setup.name, "%s", WAKE_NAME);
    setup.id.bustype = BUS_HOST;
    setup.id.vendor = 0x0001;
    setup.id.product = 0x0003;
    if (ioctl(descriptor, UI_SET_EVBIT, EV_KEY) < 0 ||
        ioctl(descriptor, UI_SET_KEYBIT, KEY_WAKEUP) < 0 ||
        ioctl(descriptor, UI_SET_PHYS, WAKE_PHYS) < 0 ||
        ioctl(descriptor, UI_DEV_SETUP, &setup) < 0 ||
        ioctl(descriptor, UI_DEV_CREATE) < 0) {
        relay_log_errno("cannot create touch wake device");
        close(descriptor);
        return -1;
    }
    return descriptor;
}

int uinput_emit_wake(int fd) {
    struct input_event events[4];

    if (fd < 0)
        return -1;
    memset(events, 0, sizeof events);
    events[0].type = events[2].type = EV_KEY;
    events[0].code = events[2].code = KEY_WAKEUP;
    events[0].value = 1;
    events[1].type = events[3].type = EV_SYN;
    events[1].code = events[3].code = SYN_REPORT;
    return write(fd, events, sizeof events) == (ssize_t)sizeof events ? 0 : -1;
}

int uinput_create_switch(void) {
    struct uinput_setup setup;
    int descriptor;

    /* /dev/uinput is root-managed, but keep the same hardening as the event
     * discovery paths (no symlink following, character device check). */
    descriptor = relay_open_character_device("/dev/uinput",
                                             O_WRONLY | O_NONBLOCK);
    if (descriptor < 0) {
        relay_log_errno("open /dev/uinput");
        return -1;
    }

    memset(&setup, 0, sizeof setup);
    snprintf(setup.name, sizeof setup.name, "%s", VIRTUAL_NAME);
    setup.id.bustype = BUS_HOST;
    setup.id.vendor = 0x0001;
    setup.id.product = 0x0002;

    if (ioctl(descriptor, UI_SET_EVBIT, EV_SW) < 0 ||
        ioctl(descriptor, UI_SET_SWBIT, SW_TABLET_MODE) < 0 ||
        ioctl(descriptor, UI_SET_PHYS, VIRTUAL_PHYS) < 0 ||
        ioctl(descriptor, UI_DEV_SETUP, &setup) < 0 ||
        ioctl(descriptor, UI_DEV_CREATE) < 0) {
        relay_log_errno("cannot set up the virtual switch");
        close(descriptor);
        return -1;
    }
    return descriptor;
}

int uinput_emit_switch(int fd, int value) {
    struct input_event events[2];
    ssize_t written;

    if (fd < 0)
        return -1;
    memset(events, 0, sizeof events);
    events[0].type = EV_SW;
    events[0].code = SW_TABLET_MODE;
    events[0].value = value ? 1 : 0;
    events[1].type = EV_SYN;
    events[1].code = SYN_REPORT;
    events[1].value = 0;

    written = write(fd, events, sizeof events);
    if (written != (ssize_t)sizeof events) {
        relay_log_errno("write to the virtual switch");
        return -1;
    }
    return 0;
}

void uinput_destroy(int fd) {
    if (fd < 0)
        return;
    (void)ioctl(fd, UI_DEV_DESTROY);
    close(fd);
}
