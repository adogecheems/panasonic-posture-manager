#ifndef PPM_SWITCH_H
#define PPM_SWITCH_H

/* SW_TABLET_MODE lives in linux/input-event-codes.h; mirrored here so the
 * capability arithmetic in switch.c stays readable. */
#define PPM_SW_TABLET_MODE 0x01

/* Result of one physical-switch enumeration. */
struct switch_hardware {
    int present; /* number of safe physical tablet switches: 0, 1 or > 1 */
    int value;   /* 0/1 when present == 1 and readable, otherwise -1 */
    int error;   /* 1 when a matching switch existed but its state failed */
};

/*
 * Capability-only verification of an open event device.  A safe aggregation
 * source carries EV_SW and no EV_KEY, and exactly SW_TABLET_MODE.  The
 * relay's own uinput device is always rejected; when wanted != NULL the
 * device name must match exactly.
 */
int switch_is_tablet(int fd, const char *wanted);

/*
 * Enumerate /dev/input/event* and fill out with the physical tablet switch
 * count and state.  Returns 0 when enumeration itself succeeded.
 */
int switch_read_hardware(const char *wanted, struct switch_hardware *out);

/*
 * Open the unique, verified physical tablet switch.  Returns a new fd, or -1
 * when the device is absent, ambiguous or unreadable.  The caller owns the
 * fd and performs the EVIOCGRAB.
 */
int switch_monitor(const char *wanted);

/* Current SW_TABLET_MODE state of an open switch: 0/1, or -1 on error. */
int switch_read_state(int fd);

/* Parse "/dev/input/eventN" into N.  Returns 0 on success, -1 otherwise. */
int switch_event_index(const char *path, int *index);

#endif /* PPM_SWITCH_H */
