/*
 * Physical tablet-mode switch discovery.
 *
 * A safe aggregation source contains EV_SW but no EV_KEY and exactly the
 * tablet switch bit: other switch devices may carry lid/power events and
 * grabbing those would suppress unrelated input.  The relay's own uinput
 * switch is excluded by its fixed name/phys so the program never reads its
 * own output as hardware.
 */
#include "switch.h"

#include <glob.h>
#include <linux/input.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include "common.h"
#include "uinput.h"

#ifndef SW_TABLET_MODE
#define SW_TABLET_MODE 0x01
#endif

int switch_event_index(const char *path, int *index) {
    return relay_event_index(path, index);
}

int switch_is_tablet(int fd, const char *wanted) {
    char name[256];
    char phys[256];
    unsigned long types = 0;
    unsigned long switches = 0;

    if (fd < 0)
        return 0;
    memset(name, 0, sizeof name);
    memset(phys, 0, sizeof phys);
    if (ioctl(fd, EVIOCGNAME(sizeof name), name) < 0)
        return 0;
    name[sizeof name - 1] = '\0';
    if (ioctl(fd, EVIOCGPHYS(sizeof phys), phys) < 0)
        phys[0] = '\0';
    phys[sizeof phys - 1] = '\0';

    if (uinput_is_own(name, phys))
        return 0;
    if (wanted != NULL && strcmp(name, wanted) != 0)
        return 0;

    if (ioctl(fd, EVIOCGBIT(0, sizeof types), &types) < 0)
        return 0;
    if ((types & (1UL << EV_SW)) == 0 || (types & (1UL << EV_KEY)) != 0)
        return 0;
    if (ioctl(fd, EVIOCGBIT(EV_SW, sizeof switches), &switches) < 0)
        return 0;
    return switches == (1UL << SW_TABLET_MODE);
}

int switch_read_state(int fd) {
    unsigned long switches = 0;

    if (fd < 0)
        return -1;
    if (ioctl(fd, EVIOCGSW(sizeof switches), &switches) < 0)
        return -1;
    return (switches & (1UL << SW_TABLET_MODE)) != 0 ? 1 : 0;
}

int switch_read_hardware(const char *wanted, struct switch_hardware *out) {
    glob_t paths;
    size_t index;
    int returned;

    if (out == NULL)
        return -1;
    out->present = 0;
    out->value = -1;
    out->error = 0;

    memset(&paths, 0, sizeof paths);
    returned = glob("/dev/input/event*", 0, NULL, &paths);
    if (returned == GLOB_NOMATCH)
        return 0;
    if (returned != 0) {
        out->error = 1;
        return -1;
    }

    for (index = 0; index < paths.gl_pathc; index++) {
        const char *device_path = paths.gl_pathv[index];
        int state;
        int descriptor;

        if (device_path == NULL)
            continue;
        descriptor = relay_open_event_device(device_path);
        if (descriptor < 0)
            continue;
        /* relay_identity_matches_sysfs also protects against a recycled node. */
        if (switch_is_tablet(descriptor, wanted) &&
            relay_identity_matches_sysfs(descriptor, device_path)) {
            out->present++;
            state = switch_read_state(descriptor);
            if (state < 0)
                out->error = 1;
            else
                out->value = state;
        }
        close(descriptor);
    }
    globfree(&paths);

    if (out->present != 1)
        out->value = -1;
    return 0;
}

int switch_monitor(const char *wanted) {
    glob_t paths;
    size_t index;
    int returned;
    int found = -1;
    int ambiguous = 0;

    memset(&paths, 0, sizeof paths);
    returned = glob("/dev/input/event*", 0, NULL, &paths);
    if (returned == GLOB_NOMATCH)
        return -1;
    if (returned != 0)
        return -1;

    for (index = 0; index < paths.gl_pathc; index++) {
        const char *device_path = paths.gl_pathv[index];
        int descriptor;

        if (device_path == NULL)
            continue;
        descriptor = relay_open_event_device(device_path);
        if (descriptor < 0)
            continue;
        if (!switch_is_tablet(descriptor, wanted) ||
            !relay_identity_matches_sysfs(descriptor, device_path)) {
            close(descriptor);
            continue;
        }
        if (found >= 0) {
            close(found);
            close(descriptor);
            found = -1;
            ambiguous = 1;
            break;
        }
        found = descriptor;
    }
    globfree(&paths);

    if (ambiguous)
        return -1;
    return found;
}
