#ifndef PPM_INPUT_H
#define PPM_INPUT_H

#define PPM_INPUT_DEVICES 2

/* input_acquire() results.  BUSY means the devices were found but a key or
 * touchpad contact is still active; this is a normal, retryable wait rather
 * than a user-visible grab failure. */
#define PPM_INPUT_ACQUIRE_OK 0
#define PPM_INPUT_ACQUIRE_FAILED -1
#define PPM_INPUT_ACQUIRE_BUSY -2

enum ppm_input_slot {
    PPM_INPUT_KEYBOARD = 0,
    PPM_INPUT_TOUCHPAD = 1,
};

/* Open fds and their expected identities for the internal input devices held
 * by the relay.  fds are -1 when not held. */
struct input_set {
    int fds[PPM_INPUT_DEVICES];
    char names[PPM_INPUT_DEVICES][256];
    char phys[PPM_INPUT_DEVICES][256];
};

void input_init(struct input_set *set);

/* Ungrab and close every held device; safe to call repeatedly. */
void input_release(struct input_set *set);

/* 1 when both internal devices are held. */
int input_ready(const struct input_set *set);

/*
 * Discover and grab the internal keyboard and touchpad.  Both are required:
 * when either cannot be found, verified or grabbed, every fd is released and
 * -1 is returned so the relay can retry later without exiting the main loop.
 * -2 means the devices were found but are still in use; the caller retries
 * silently instead of reporting a grab error.
 * Profile-supplied names are still capability-checked and USB/Bluetooth
 * devices are never grabbed.
 */
int input_acquire(struct input_set *set, const char *keyboard_name,
                  const char *touchpad_name);

/* 1 when a held device vanished, was replaced or is no longer ready.  Uses
 * EVIOCGNAME/EVIOCGPHYS and the capability predicate to catch a same-name
 * hotplug replacement. */
int input_lost(const struct input_set *set);

/* Passive observation only; never grabs the touchscreen. Rejects ambiguous
 * discovery. Refresh preserves the existing fd for the same device, otherwise
 * closes it. A negative result leaves wake unavailable, not input grabbed. */
int input_refresh_touchscreen(int current);

/* Read a bounded batch and report only a contact start, never coordinates.
 * SYN_DROPPED suppresses events until the next SYN_REPORT. -1 means lost fd. */
int input_touch_activity(int fd, int *desynchronized);

#endif /* PPM_INPUT_H */
