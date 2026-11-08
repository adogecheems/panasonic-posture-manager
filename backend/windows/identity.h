/* Device-bus admission, shared by the filter and host-side boundary tests. */
#ifndef POSTURE_WINDOWS_IDENTITY_H
#define POSTURE_WINDOWS_IDENTITY_H

enum posture_bus { POSTURE_BUS_OTHER, POSTURE_BUS_ACPI, POSTURE_BUS_INTERNAL, POSTURE_BUS_HID };

static inline int posture_accept_bus(int role, enum posture_bus bus,
                                    int device_is_acpi, int parent_matches,
                                    int parent_is_acpi) {
    if (role != 0 && role != 1) return 0;
    if (bus == POSTURE_BUS_ACPI || bus == POSTURE_BUS_INTERNAL)
        return device_is_acpi;
    /* A HID mouse collection is internal only with the exact enrolled ACPI
     * parent. USB/Bluetooth ancestry and HID keyboards are never admitted. */
    return role == 1 && bus == POSTURE_BUS_HID && parent_matches && parent_is_acpi;
}
#endif
