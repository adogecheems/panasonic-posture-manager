/* SPDX-License-Identifier: AGPL-3.0-or-later */
#include <assert.h>
#include <stdio.h>
#include "../control.h"

int main(void) {
    struct posture_guard guard = {0};
    struct posture_connection connection = {0};
    struct posture_request request = {POSTURE_CONTROL_VERSION, sizeof request,
                                     POSTURE_ACQUIRE, 0, 1, 100, 200};
    struct posture_reply reply;
    posture_command(&guard, &connection, &request, 1000, &reply);
    assert(reply.status == POSTURE_NOT_READY && !guard.blocked);
    guard.ready_mask = POSTURE_READY_ALL;
    guard.busy_mask = 1;
    ++request.serial;
    posture_command(&guard, &connection, &request, 1001, &reply);
    assert(reply.status == POSTURE_BUSY && !guard.blocked);
    guard.busy_mask = 0;
    ++request.serial;
    posture_command(&guard, &connection, &request, 1002, &reply);
    assert(reply.status == POSTURE_OK && reply.flags == POSTURE_BLOCKED);
    assert(reply.remaining_ms == POSTURE_LEASE_MS);
    request.operation = POSTURE_QUERY;
    ++request.serial;
    posture_command(&guard, &connection, &request, 4001, &reply);
    assert(reply.remaining_ms == 1);
    assert(!posture_blocked(&guard, 4002));
    request.operation = POSTURE_RENEW;
    ++request.serial;
    posture_command(&guard, &connection, &request, 4003, &reply);
    assert(reply.status == POSTURE_EXPIRED && !guard.blocked);
    request.operation = POSTURE_ACQUIRE;
    ++request.serial;
    posture_command(&guard, &connection, &request, 4004, &reply);
    assert(guard.blocked);
    request.operation = POSTURE_RENEW;
    ++request.serial;
    posture_command(&guard, &connection, &request, 5000, &reply);
    assert(guard.deadline == 8000);
    /* A repeated sequence cannot renew, and fails open immediately. */
    posture_command(&guard, &connection, &request, 6000, &reply);
    assert(reply.status == POSTURE_INVALID && !guard.blocked);
    request.operation = POSTURE_ACQUIRE;
    ++request.serial;
    posture_command(&guard, &connection, &request, 6001, &reply);
    assert(guard.blocked);
    guard.ready_mask = POSTURE_READY_KEYBOARD;
    assert(!posture_blocked(&guard, 6002));
    guard.ready_mask = POSTURE_READY_ALL;
    ++request.serial;
    posture_command(&guard, &connection, &request, UINT64_MAX - 1, &reply);
    assert(reply.status == POSTURE_INVALID && !guard.blocked);
    ++request.serial;
    posture_command(&guard, &connection, &request, 7000, &reply);
    assert(guard.blocked);
    request.nonce_high = 201;
    ++request.serial;
    posture_command(&guard, &connection, &request, 7001, &reply);
    assert(reply.status == POSTURE_INVALID && !guard.blocked);
    posture_release(&guard);
    assert(!guard.blocked && guard.deadline == 0);
    puts("Windows control lease tests passed");
    return 0;
}
