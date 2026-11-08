/* SPDX-License-Identifier: AGPL-3.0-or-later
 * Windows-only driver control v1. No input contents cross this boundary.
 */
#ifndef POSTURE_WINDOWS_CONTROL_H
#define POSTURE_WINDOWS_CONTROL_H
#include <stdint.h>

#define POSTURE_CONTROL_VERSION 1u
#define POSTURE_LEASE_MS 3000u
#define POSTURE_READY_KEYBOARD 1u
#define POSTURE_READY_TOUCHPAD 2u
#define POSTURE_READY_ALL 3u
#define POSTURE_BLOCKED 1u
#define POSTURE_QUERY 1u
#define POSTURE_ACQUIRE 2u
#define POSTURE_RENEW 3u
#define POSTURE_RELEASE 4u
#define POSTURE_OK 0u
#define POSTURE_INVALID 1u
#define POSTURE_NOT_READY 2u
#define POSTURE_BUSY 3u
#define POSTURE_EXPIRED 4u

struct posture_request {
    uint32_t version, size, operation, reserved;
    uint64_t serial, nonce_low, nonce_high;
};
struct posture_reply {
    uint32_t version, size, status, flags;
    uint64_t serial, nonce_low, nonce_high;
    uint32_t ready_mask, remaining_ms, busy_mask, reserved;
};
struct posture_connection {
    uint64_t serial, nonce_low, nonce_high;
};
struct posture_guard {
    uint64_t deadline;
    uint32_t ready_mask, busy_mask;
    int blocked;
};

void posture_release(struct posture_guard *guard);
int posture_blocked(struct posture_guard *guard, uint64_t now_ms);
void posture_command(struct posture_guard *guard, struct posture_connection *connection,
                     const struct posture_request *request, uint64_t now_ms,
                     struct posture_reply *reply);
#endif
