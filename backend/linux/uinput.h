#ifndef PPM_UINPUT_H
#define PPM_UINPUT_H

/*
 * Create the relay-owned virtual tablet-mode switch.  The device is named
 * VIRTUAL_NAME and tagged with UI_SET_PHYS VIRTUAL_PHYS so every discovery
 * path can identify and exclude it.  Returns the uinput fd or -1.
 */
int uinput_create_switch(void);

/* Emit one SW_TABLET_MODE value (0/1) followed by SYN_REPORT.  0 or -1. */
int uinput_emit_switch(int fd, int value);

/* A separate wake-only device: no power key, characters or pointer events. */
int uinput_create_wake(void);
int uinput_emit_wake(int fd);

/* Destroy the virtual device (UI_DEV_DESTROY) and close the fd.  A negative
 * descriptor is ignored, so cleanup paths can call this unconditionally. */
void uinput_destroy(int fd);

/* Whether a device name/phys belongs to the relay's own virtual switch. */
int uinput_is_own(const char *name, const char *phys);

#endif /* PPM_UINPUT_H */
