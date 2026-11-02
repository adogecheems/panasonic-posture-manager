/*
 * Internal keyboard/touchpad discovery and grabbing.
 *
 * Discovery is capability-based and never trusts device names on its own:
 * external USB/Bluetooth devices are rejected by bus and phys, and a profile
 * supplied exact name is still capability-checked.  A grab failure is not
 * fatal: the caller retries while the aggregate tablet mode stays on.
 */
#include "input.h"

#include <glob.h>
#include <linux/input.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#include "common.h"
#include "uinput.h"

/* Case-insensitive substring search used for phys/name heuristics. */
static int contains_ci(const char *haystack, const char *needle) {
    size_t needle_length;

    if (haystack == NULL || needle == NULL || needle[0] == '\0')
        return 0;
    needle_length = strlen(needle);
    while (*haystack != '\0') {
        size_t offset = 0;

        while (offset < needle_length && haystack[offset] != '\0') {
            char left = haystack[offset];
            char right = needle[offset];

            if (left >= 'A' && left <= 'Z')
                left = (char)(left - 'A' + 'a');
            if (right >= 'A' && right <= 'Z')
                right = (char)(right - 'A' + 'a');
            if (left != right)
                break;
            offset++;
        }
        if (offset == needle_length)
            return 1;
        haystack++;
    }
    return 0;
}

static int has_key(int fd, int code) {
    unsigned char bits[(KEY_CNT + 7) / 8];

    memset(bits, 0, sizeof bits);
    if (ioctl(fd, EVIOCGBIT(EV_KEY, sizeof bits), bits) < 0)
        return 0;
    return (bits[code / 8] & (1 << (code % 8))) != 0;
}

static int has_abs(int fd, int code) {
    unsigned char bits[(ABS_CNT + 7) / 8];

    memset(bits, 0, sizeof bits);
    if (ioctl(fd, EVIOCGBIT(EV_ABS, sizeof bits), bits) < 0)
        return 0;
    return (bits[code / 8] & (1 << (code % 8))) != 0;
}

static int has_prop(int fd, int prop) {
    unsigned long bits = 0;

    if (ioctl(fd, EVIOCGPROP(sizeof bits), &bits) < 0)
        return 0;
    return (bits & (1UL << prop)) != 0;
}

static int external_device(const struct input_id *id, const char *phys) {
    if (id->bustype == BUS_USB || id->bustype == BUS_BLUETOOTH)
        return 1;
    return contains_ci(phys, "usb") || contains_ci(phys, "bluetooth");
}

/* 1 when no key is currently pressed on the device. */
static int keys_idle(int fd) {
    unsigned char keys[(KEY_CNT + 7) / 8];
    size_t index;

    memset(keys, 0, sizeof keys);
    if (ioctl(fd, EVIOCGKEY(sizeof keys), keys) < 0)
        return 0;
    for (index = 0; index < sizeof keys; index++) {
        if (keys[index] != 0)
            return 0;
    }
    return 1;
}

struct device_info {
    char name[256];
    char phys[256];
    struct input_id id;
    int own;
};

static int read_device_info(int fd, struct device_info *info) {
    memset(info, 0, sizeof *info);
    if (ioctl(fd, EVIOCGNAME(sizeof info->name), info->name) < 0)
        return -1;
    info->name[sizeof info->name - 1] = '\0';
    if (ioctl(fd, EVIOCGPHYS(sizeof info->phys), info->phys) < 0)
        info->phys[0] = '\0';
    info->phys[sizeof info->phys - 1] = '\0';
    if (ioctl(fd, EVIOCGID, &info->id) < 0)
        return -1;
    info->own = uinput_is_own(info->name, info->phys);
    return 0;
}

static int keyboard_candidate(int fd, const struct device_info *info,
                              const char *wanted) {
    if (info->own || external_device(&info->id, info->phys))
        return 0;
    if (wanted != NULL) {
        /* A profile name only disambiguates; capabilities and the external
         * bus/phys exclusion above still apply. */
        if (strcmp(info->name, wanted) != 0)
            return 0;
    } else {
        if (info->phys[0] == '\0')
            return 0;
        if (!contains_ci(info->name, "keyboard") &&
            !contains_ci(info->phys, "isa") &&
            !contains_ci(info->phys, "i8042"))
            return 0;
    }
    return has_key(fd, KEY_A) && has_key(fd, KEY_Z);
}

static int touchpad_candidate(int fd, const struct device_info *info,
                              const char *wanted) {
    if (info->own || external_device(&info->id, info->phys))
        return 0;
    if (wanted != NULL) {
        if (strcmp(info->name, wanted) != 0)
            return 0;
    } else {
        if (info->phys[0] == '\0')
            return 0;
        if (!has_prop(fd, INPUT_PROP_POINTER) &&
            !has_prop(fd, INPUT_PROP_BUTTONPAD) &&
            !contains_ci(info->phys, "i2c"))
            return 0;
    }
    return has_abs(fd, ABS_MT_POSITION_X) &&
           !has_prop(fd, INPUT_PROP_DIRECT);
}

static int slot_candidate(int fd, const struct device_info *info,
                          int slot, const char *wanted) {
    if (slot == PPM_INPUT_KEYBOARD)
        return keyboard_candidate(fd, info, wanted);
    return touchpad_candidate(fd, info, wanted);
}

void input_init(struct input_set *set) {
    int slot;

    if (set == NULL)
        return;
    memset(set, 0, sizeof *set);
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++)
        set->fds[slot] = -1;
}

void input_release(struct input_set *set) {
    int slot;

    if (set == NULL)
        return;
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (set->fds[slot] >= 0) {
            (void)ioctl(set->fds[slot], EVIOCGRAB, 0);
            close(set->fds[slot]);
            set->fds[slot] = -1;
        }
        memset(set->names[slot], 0, sizeof set->names[slot]);
        memset(set->phys[slot], 0, sizeof set->phys[slot]);
    }
}

int input_ready(const struct input_set *set) {
    int slot;

    if (set == NULL)
        return 0;
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (set->fds[slot] < 0)
            return 0;
    }
    return 1;
}

int input_acquire(struct input_set *set, const char *keyboard_name,
                  const char *touchpad_name) {
    glob_t paths;
    struct device_info infos[PPM_INPUT_DEVICES];
    int found[PPM_INPUT_DEVICES];
    int returned;
    int slot;
    size_t index;
    int success = 0;
    int status = PPM_INPUT_ACQUIRE_FAILED;

    if (set == NULL)
        return -1;
    input_release(set);
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        found[slot] = -1;
        memset(&infos[slot], 0, sizeof infos[slot]);
    }

    memset(&paths, 0, sizeof paths);
    returned = glob("/dev/input/event*", 0, NULL, &paths);
    if (returned != 0) {
        if (returned != GLOB_NOMATCH)
            relay_log_errno("glob /dev/input/event*");
        return -1;
    }

    for (index = 0; index < paths.gl_pathc; index++) {
        const char *device_path = paths.gl_pathv[index];
        struct device_info info;
        int descriptor;
        int candidate;

        if (device_path == NULL)
            continue;
        descriptor = relay_open_event_device(device_path);
        if (descriptor < 0)
            continue;
        if (read_device_info(descriptor, &info) < 0 ||
            !relay_identity_matches_sysfs(descriptor, device_path)) {
            /* Reject a recycled/renamed node instead of grabbing it. */
            close(descriptor);
            continue;
        }
        if (keyboard_candidate(descriptor, &info, keyboard_name))
            candidate = PPM_INPUT_KEYBOARD;
        else if (touchpad_candidate(descriptor, &info, touchpad_name))
            candidate = PPM_INPUT_TOUCHPAD;
        else
            candidate = -1;
        if (candidate < 0) {
            close(descriptor);
            continue;
        }
        if (found[candidate] >= 0) {
            /* Ambiguous match: refuse to guess. */
            relay_log("ambiguous internal input device for slot %d", candidate);
            close(descriptor);
            goto cleanup;
        }
        found[candidate] = descriptor;
        infos[candidate] = info;
    }

    if (found[PPM_INPUT_KEYBOARD] < 0 || found[PPM_INPUT_TOUCHPAD] < 0)
        goto cleanup;

    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (!keys_idle(found[slot])) {
            status = PPM_INPUT_ACQUIRE_BUSY;
            goto cleanup;
        }
    }
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (ioctl(found[slot], EVIOCGRAB, 1) < 0) {
            relay_log_errno("EVIOCGRAB");
            while (slot > 0) {
                slot--;
                (void)ioctl(found[slot], EVIOCGRAB, 0);
            }
            goto cleanup;
        }
    }
    /*
     * Keys can be pressed between the pre-grab check and EVIOCGRAB.  Their
     * release events would then be delivered to the relay instead of libinput,
     * leaving a stuck key: re-check after grabbing and back off if needed.
     */
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (!keys_idle(found[slot])) {
            relay_log("keys pressed while grabbing; releasing grabs");
            while (slot >= 0) {
                (void)ioctl(found[slot], EVIOCGRAB, 0);
                slot--;
            }
            status = PPM_INPUT_ACQUIRE_BUSY;
            goto cleanup;
        }
    }

    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        set->fds[slot] = found[slot];
        found[slot] = -1;
        memcpy(set->names[slot], infos[slot].name, sizeof set->names[slot]);
        set->names[slot][sizeof set->names[slot] - 1] = '\0';
        memcpy(set->phys[slot], infos[slot].phys, sizeof set->phys[slot]);
        set->phys[slot][sizeof set->phys[slot] - 1] = '\0';
    }
    success = 1;

cleanup:
    globfree(&paths);
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (found[slot] >= 0)
            close(found[slot]);
    }
    if (!success) {
        input_release(set);
        return status;
    }
    return PPM_INPUT_ACQUIRE_OK;
}

int input_lost(const struct input_set *set) {
    int slot;

    if (set == NULL || !input_ready(set))
        return 1;
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        struct device_info info;

        if (read_device_info(set->fds[slot], &info) < 0)
            return 1;
        if (info.own || strcmp(info.name, set->names[slot]) != 0)
            return 1;
        if (set->phys[slot][0] != '\0' && info.phys[0] != '\0' &&
            strcmp(info.phys, set->phys[slot]) != 0)
            return 1;
        /* Same-name replacement: re-run the capability predicate as well. */
        if (!slot_candidate(set->fds[slot], &info, slot, set->names[slot]))
            return 1;
    }
    return 0;
}
