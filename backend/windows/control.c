/* SPDX-License-Identifier: AGPL-3.0-or-later */
#include "control.h"

_Static_assert(sizeof(struct posture_request) == 40, "control request layout");
_Static_assert(sizeof(struct posture_reply) == 56, "control reply layout");

void posture_release(struct posture_guard *guard) {
    guard->blocked = 0;
    guard->deadline = 0;
}

int posture_blocked(struct posture_guard *guard, uint64_t now_ms) {
    if (guard->ready_mask != POSTURE_READY_ALL || now_ms >= guard->deadline)
        posture_release(guard);
    return guard->blocked;
}

void posture_command(struct posture_guard *guard, struct posture_connection *connection,
                     const struct posture_request *request, uint64_t now_ms,
                     struct posture_reply *reply) {
    uint32_t status = POSTURE_OK;
    (void)posture_blocked(guard, now_ms);
    if (request->version != POSTURE_CONTROL_VERSION || request->size != sizeof *request ||
        request->reserved || !request->serial || request->serial <= connection->serial ||
        (!request->nonce_low && !request->nonce_high) ||
        (connection->serial && (request->nonce_low != connection->nonce_low ||
                                request->nonce_high != connection->nonce_high)) ||
        request->operation < POSTURE_QUERY || request->operation > POSTURE_RELEASE ||
        now_ms > UINT64_MAX - POSTURE_LEASE_MS) {
        status = POSTURE_INVALID;
        posture_release(guard);
    } else {
        connection->serial = request->serial;
        connection->nonce_low = request->nonce_low;
        connection->nonce_high = request->nonce_high;
        switch (request->operation) {
        case POSTURE_ACQUIRE:
            if (guard->ready_mask != POSTURE_READY_ALL)
                status = POSTURE_NOT_READY;
            else if (guard->busy_mask)
                status = POSTURE_BUSY;
            else if (guard->blocked)
                status = POSTURE_INVALID;
            else {
                guard->blocked = 1;
                guard->deadline = now_ms + POSTURE_LEASE_MS;
            }
            if (status != POSTURE_OK)
                posture_release(guard);
            break;
        case POSTURE_RENEW:
            if (!guard->blocked)
                status = POSTURE_EXPIRED;
            else
                guard->deadline = now_ms + POSTURE_LEASE_MS;
            break;
        case POSTURE_RELEASE:
            posture_release(guard);
            break;
        case POSTURE_QUERY:
            break;
        }
    }
    reply->version = POSTURE_CONTROL_VERSION;
    reply->size = sizeof *reply;
    reply->status = status;
    reply->flags = guard->blocked ? POSTURE_BLOCKED : 0;
    reply->serial = request->serial;
    reply->nonce_low = request->nonce_low;
    reply->nonce_high = request->nonce_high;
    reply->ready_mask = guard->ready_mask;
    reply->remaining_ms = guard->blocked ? (uint32_t)(guard->deadline - now_ms) : 0;
    reply->busy_mask = guard->busy_mask;
    reply->reserved = 0;
}
