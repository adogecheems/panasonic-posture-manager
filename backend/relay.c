/*
 * Privileged switch/input actuator for Panasonic 360-degree convertibles.
 *
 * The system service keeps a 3 s software lease alive by writing heartbeat
 * bytes to stdin.  The relay aggregates the physical SW_TABLET_MODE switch
 * with that software request and exposes exactly one switch-only uinput
 * device to GNOME.  While the aggregate is ON it also grabs the internal
 * keyboard and touchpad; a grab failure is retried and reported instead of
 * killing the main loop.
 *
 * stdin  (v2): byte '1' = heartbeat (refreshes the 3 s lease), '\n' is
 *              ignored, any other byte is a protocol error; EOF or lease
 *              expiry releases.
 * stdout (v2): one JSON object per line, see common.h / agent.md:
 *              {"protocol":2,"kind":"ready","physical":bool}
 *              {"protocol":2,"kind":"physical","present":bool}
 *              {"protocol":2,"kind":"applied",...}
 *              {"protocol":2,"kind":"released","exit":int}
 */
#include "common.h"
#include "input.h"
#include "switch.h"
#include "uinput.h"

#include <linux/input.h>
#include <signal.h>
#include <sys/ioctl.h>
#include <sys/poll.h>
#include <sys/prctl.h>
#include <time.h>

#ifndef SW_TABLET_MODE
#define SW_TABLET_MODE 0x01
#endif

#define RELAY_ERROR_NONE 0
#define RELAY_ERROR_INPUT_GRAB 1

static volatile sig_atomic_t stopping;

static void handle_signal(int signal_number) {
    (void)signal_number;
    stopping = 1;
}

static double monotonic_now(void) {
    struct timespec value;

    if (clock_gettime(CLOCK_MONOTONIC, &value) < 0) {
        /* CLOCK_MONOTONIC cannot fail on Linux; keep the loop bounded anyway. */
        return 0.0;
    }
    return (double)value.tv_sec + (double)value.tv_nsec / 1e9;
}

static void sleep_milliseconds(int milliseconds) {
    struct timespec request;

    request.tv_sec = milliseconds / 1000;
    request.tv_nsec = (long)(milliseconds % 1000) * 1000000L;
    while (nanosleep(&request, &request) < 0 && errno == EINTR && !stopping) {
        /* Resume the remaining delay. */
    }
}

struct options {
    const char *switch_name;
    const char *keyboard_name;
    const char *touchpad_name;
    int recover;
};

static int parse_options(int argc, char **argv, struct options *options) {
    int index;

    memset(options, 0, sizeof *options);
    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--recover") == 0) {
            options->recover = 1;
        } else if (strcmp(argv[index], "--switch-name") == 0 && index + 1 < argc) {
            options->switch_name = argv[++index];
        } else if (strcmp(argv[index], "--keyboard-name") == 0 && index + 1 < argc) {
            options->keyboard_name = argv[++index];
        } else if (strcmp(argv[index], "--touchpad-name") == 0 && index + 1 < argc) {
            options->touchpad_name = argv[++index];
        } else {
            relay_log("unknown or incomplete argument %s", argv[index]);
            return -1;
        }
    }
    return 0;
}

/* --- JSON protocol (stdout, one object per line) -------------------------- */

static void emit_ready(int physical) {
    printf("{\"protocol\":%d,\"kind\":\"ready\",\"physical\":%s}\n",
           RELAY_PROTOCOL, physical ? "true" : "false");
}

static void emit_physical(int present) {
    printf("{\"protocol\":%d,\"kind\":\"physical\",\"present\":%s}\n",
           RELAY_PROTOCOL, present ? "true" : "false");
}

static void emit_released(int code) {
    printf("{\"protocol\":%d,\"kind\":\"released\",\"exit\":%d}\n",
           RELAY_PROTOCOL, code);
}

/* --- Relay state ---------------------------------------------------------- */

struct relay_state {
    struct options options;
    int virtual_fd;
    int monitor_fd;
    int physical;
    int hardware_state; /* 1 when the physical switch is ON, else 0 */
    int software;       /* set once the service refreshes the lease */
    int output;         /* last value emitted through the virtual switch */
    int reported_hardware;
    int reported_physical;
    int reported_error;
    int reported_input;
    int reported_once;
    int input_error;
    int input_waiting;
    int attach_failures;
    int scan_failures;
    int release_requested;
    struct input_set inputs;
    double lease;
    double next_scan;
    double next_input;
    double last_report;
};

static void relay_init(struct relay_state *relay, const struct options *options) {
    memset(relay, 0, sizeof *relay);
    relay->options = *options;
    relay->virtual_fd = -1;
    relay->monitor_fd = -1;
    relay->hardware_state = 0;
    relay->output = -1;
    relay->reported_hardware = -1;
    relay->reported_physical = -1;
    relay->reported_error = -1;
    relay->reported_input = -1;
    relay->input_error = RELAY_ERROR_NONE;
    relay->input_waiting = 0;
    input_init(&relay->inputs);
}

static int relay_input_disabled(const struct relay_state *relay) {
    int desired = relay->hardware_state || relay->software;
    return desired && input_ready(&relay->inputs);
}

static void emit_applied(const struct relay_state *relay) {
    int desired = relay->hardware_state || relay->software;
    int input_disabled = relay_input_disabled(relay);
    int input_error = !input_disabled && desired &&
                      relay->input_error == RELAY_ERROR_INPUT_GRAB;

    printf("{\"protocol\":%d,\"kind\":\"applied\",\"hardware\":%s,"
           "\"tablet\":%s,\"input_disabled\":%s,\"physical\":%s,"
           "\"error\":%s}\n",
           RELAY_PROTOCOL,
           relay->hardware_state ? "true" : "false",
           desired ? "true" : "false",
           input_disabled ? "true" : "false",
           relay->physical ? "true" : "false",
           input_error
                   ? "\"input-grab-failed\""
                   : "null");
}

static void emit_if_changed(struct relay_state *relay, double now) {
    if (relay->reported_once &&
        relay->reported_hardware == relay->hardware_state &&
        relay->reported_physical == relay->physical &&
        relay->reported_error == relay->input_error &&
        relay->reported_input == relay_input_disabled(relay) &&
        now - relay->last_report < RELAY_REPORT_SECONDS)
        return;
    emit_applied(relay);
    relay->reported_hardware = relay->hardware_state;
    relay->reported_physical = relay->physical;
    relay->reported_error = relay->input_error;
    relay->reported_input = relay_input_disabled(relay);
    relay->reported_once = 1;
    relay->last_report = now;
}

/* --- Physical switch ownership ------------------------------------------- */

static void release_monitor(struct relay_state *relay, int announce) {
    if (relay->monitor_fd >= 0) {
        (void)ioctl(relay->monitor_fd, EVIOCGRAB, 0);
        close(relay->monitor_fd);
        relay->monitor_fd = -1;
    }
    relay->hardware_state = 0;
    relay->output = -1;
    relay->reported_hardware = -1;
    relay->reported_physical = -1;
    relay->reported_input = -1;
    relay->reported_once = 0;
    if (relay->physical) {
        relay->physical = 0;
        if (announce)
            emit_physical(0);
    }
}

static int attach_monitor(struct relay_state *relay, int announce) {
    int state;
    int candidate;

    candidate = switch_monitor(relay->options.switch_name);
    if (candidate < 0)
        return -1;
    if (ioctl(candidate, EVIOCGRAB, 1) < 0) {
        relay_log_errno("cannot grab the physical tablet switch");
        close(candidate);
        return -1;
    }
    relay->monitor_fd = candidate;
    relay->physical = 1;
    state = switch_read_state(candidate);
    relay->hardware_state = state >= 0 ? state : 0;
    relay->output = -1;
    relay->reported_hardware = -1;
    relay->reported_physical = -1;
    relay->reported_input = -1;
    relay->reported_once = 0;
    relay_log("attached to the physical tablet switch");
    if (announce)
        emit_physical(1);
    return 0;
}

/* Startup: the first enumeration already told us a switch is present.
 * One initial attempt plus RELAY_SWITCH_RETRY_LIMIT retries (6 attempts total,
 * matching the runtime "6 consecutive failures" limit). */
static int initial_attach(struct relay_state *relay) {
    int attempt;

    for (attempt = 0; attempt <= RELAY_SWITCH_RETRY_LIMIT; attempt++) {
        struct switch_hardware scan;

        if (switch_read_hardware(relay->options.switch_name, &scan) < 0 ||
            scan.error != 0 || scan.present > 1)
            return RELAY_EXIT_HARDWARE;
        if (scan.present == 0) {
            relay_log("physical tablet switch disappeared during startup");
            relay->hardware_state = 0;
            return RELAY_EXIT_OK;
        }
        if (attach_monitor(relay, 0) == 0)
            return RELAY_EXIT_OK;
        if (attempt < RELAY_SWITCH_RETRY_LIMIT)
            sleep_milliseconds(RELAY_ATTACH_RETRY_MILLISECONDS);
    }
    relay_log("physical tablet switch present but cannot be grabbed");
    return RELAY_EXIT_SWITCH;
}

/*
 * Periodic hotplug/revalidation check: enumerate the physical switches,
 * detach when the owned device changed, and attach when one appears.
 */
static int maintain_switch(struct relay_state *relay) {
    struct switch_hardware scan;

    if (switch_read_hardware(relay->options.switch_name, &scan) != 0 ||
        scan.error != 0) {
        relay->scan_failures++;
        relay_log("physical tablet switch re-read failed (%d/%d)",
                  relay->scan_failures, RELAY_SWITCH_RETRY_LIMIT);
        if (relay->scan_failures > RELAY_SWITCH_RETRY_LIMIT)
            return RELAY_EXIT_HARDWARE;
        return RELAY_EXIT_OK;
    }
    relay->scan_failures = 0;
    if (scan.present > 1) {
        relay_log("multiple physical tablet switches found; refusing to guess");
        return RELAY_EXIT_HARDWARE;
    }
    if (relay->monitor_fd >= 0) {
        if (scan.present == 0 ||
            !switch_is_tablet(relay->monitor_fd, relay->options.switch_name)) {
            relay_log("physical tablet switch disappeared or changed");
            release_monitor(relay, 1);
            relay->attach_failures = 0;
            relay->next_scan = 0.0;
        }
        return RELAY_EXIT_OK;
    }
    if (scan.present == 0) {
        relay->attach_failures = 0;
        relay->hardware_state = 0;
        return RELAY_EXIT_OK;
    }
    if (attach_monitor(relay, 1) == 0) {
        relay->attach_failures = 0;
        return RELAY_EXIT_OK;
    }
    relay->attach_failures++;
    if (relay->attach_failures > RELAY_SWITCH_RETRY_LIMIT) {
        relay_log("physical tablet switch present but cannot be grabbed");
        return RELAY_EXIT_SWITCH;
    }
    return RELAY_EXIT_OK;
}

/* --- Internal input grabbing ---------------------------------------------- */

static void maintain_input(struct relay_state *relay, double now) {
    int desired = relay->hardware_state || relay->software;
    int acquired;

    if (!desired) {
        input_release(&relay->inputs);
        relay->input_error = RELAY_ERROR_NONE;
        relay->input_waiting = 0;
        return;
    }
    if (input_ready(&relay->inputs)) {
        relay->input_waiting = 0;
        if (now >= relay->next_input) {
            relay->next_input = now + RELAY_INPUT_RECHECK_SECONDS;
            if (input_lost(&relay->inputs)) {
                relay_log("held internal input changed; releasing grabs");
                input_release(&relay->inputs);
                relay->input_error = RELAY_ERROR_INPUT_GRAB;
                relay->next_input = now + RELAY_INPUT_RETRY_SECONDS;
            }
        }
        return;
    }
    if (now < relay->next_input)
        return;
    relay->next_input = now + RELAY_INPUT_RETRY_SECONDS;
    acquired = input_acquire(&relay->inputs, relay->options.keyboard_name,
                             relay->options.touchpad_name);
    if (acquired == PPM_INPUT_ACQUIRE_BUSY) {
        if (!relay->input_waiting)
            relay_log("internal input is in use; waiting before grabbing");
        relay->input_waiting = 1;
        return;
    }
    relay->input_waiting = 0;
    if (acquired < 0) {
        if (relay->input_error != RELAY_ERROR_INPUT_GRAB)
            relay_log("cannot grab the internal keyboard/touchpad; retrying");
        relay->input_error = RELAY_ERROR_INPUT_GRAB;
    } else {
        relay_log("grabbed the internal keyboard and touchpad");
        relay->input_error = RELAY_ERROR_NONE;
    }
}

/* --- Main loop I/O -------------------------------------------------------- */

static void detach_switch(struct relay_state *relay) {
    relay_log("physical tablet switch went away; detaching");
    release_monitor(relay, 1);
    relay->attach_failures = 0;
    relay->next_scan = 0.0;
}

static int handle_stdin(struct relay_state *relay, short revents) {
    char buffer[64];
    ssize_t length;
    ssize_t index;

    if ((revents & POLLIN) != 0) {
        length = read(STDIN_FILENO, buffer, sizeof buffer);
        if (length < 0) {
            if (errno == EAGAIN || errno == EINTR)
                return RELAY_EXIT_OK;
            relay_log_errno("read stdin");
            return RELAY_EXIT_PROTOCOL;
        }
        if (length == 0) {
            relay_log("stdin closed; releasing");
            relay->release_requested = 1;
            return RELAY_EXIT_OK;
        }
        for (index = 0; index < length; index++) {
            unsigned char byte = (unsigned char)buffer[index];

            if (byte == '1') {
                relay->software = 1;
                relay->lease = monotonic_now() + RELAY_LEASE_SECONDS;
            } else if (byte != '\n') {
                relay_log("invalid stdin byte 0x%02x", (unsigned int)byte);
                return RELAY_EXIT_PROTOCOL;
            }
        }
        return RELAY_EXIT_OK;
    }
    relay_log("stdin hangup; releasing");
    relay->release_requested = 1;
    return RELAY_EXIT_OK;
}

static void handle_monitor_event(struct relay_state *relay, short revents) {
    if (relay->monitor_fd < 0)
        return;
    if ((revents & POLLIN) != 0) {
        struct input_event events[16];
        ssize_t length = read(relay->monitor_fd, events, sizeof events);

        if (length < 0) {
            if (errno != EAGAIN && errno != EINTR) {
                detach_switch(relay);
                return;
            }
        } else if (length == 0) {
            detach_switch(relay);
            return;
        }
        /* Payload is ignored: the state is re-read with EVIOCGSW. */
    }
    if ((revents & (POLLERR | POLLHUP | POLLNVAL)) != 0)
        detach_switch(relay);
}

static void handle_input_event(struct relay_state *relay) {
    relay_log("held internal input went away; releasing grabs");
    input_release(&relay->inputs);
    relay->input_error = RELAY_ERROR_INPUT_GRAB;
    relay->next_input = monotonic_now() + RELAY_INPUT_RETRY_SECONDS;
}

static int poll_events(struct relay_state *relay) {
    struct pollfd fds[1 + 1 + PPM_INPUT_DEVICES];
    nfds_t count = 0;
    int stdin_index;
    int monitor_index = -1;
    int input_index[PPM_INPUT_DEVICES];
    int ready;
    int slot;

    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++)
        input_index[slot] = -1;

    stdin_index = (int)count;
    fds[count].fd = STDIN_FILENO;
    fds[count].events = POLLIN;
    fds[count].revents = 0;
    count++;

    if (relay->monitor_fd >= 0) {
        monitor_index = (int)count;
        fds[count].fd = relay->monitor_fd;
        fds[count].events = POLLIN;
        fds[count].revents = 0;
        count++;
    }
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (relay->inputs.fds[slot] >= 0) {
            input_index[slot] = (int)count;
            fds[count].fd = relay->inputs.fds[slot];
            fds[count].events = 0;
            fds[count].revents = 0;
            count++;
        }
    }

    ready = poll(fds, count, RELAY_POLL_MILLISECONDS);
    if (ready < 0) {
        if (errno == EINTR)
            return RELAY_EXIT_OK;
        relay_log_errno("poll");
        return RELAY_EXIT_POLL;
    }
    if (ready == 0)
        return RELAY_EXIT_OK;

    if ((fds[stdin_index].revents & (POLLIN | POLLHUP | POLLERR)) != 0)
        return handle_stdin(relay, fds[stdin_index].revents);
    if (monitor_index >= 0 &&
        (fds[monitor_index].revents & (POLLIN | POLLERR | POLLHUP | POLLNVAL)) != 0)
        handle_monitor_event(relay, fds[monitor_index].revents);
    for (slot = 0; slot < PPM_INPUT_DEVICES; slot++) {
        if (input_index[slot] >= 0 &&
            (fds[input_index[slot]].revents & (POLLERR | POLLHUP | POLLNVAL)) != 0)
            handle_input_event(relay);
    }
    return RELAY_EXIT_OK;
}

/* --- Entry points --------------------------------------------------------- */

static int install_signal_handlers(void) {
    struct sigaction action;

    memset(&action, 0, sizeof action);
    action.sa_handler = handle_signal;
    sigemptyset(&action.sa_mask);
    action.sa_flags = 0;
    if (sigaction(SIGINT, &action, NULL) < 0 ||
        sigaction(SIGTERM, &action, NULL) < 0) {
        relay_log_errno("sigaction");
        return -1;
    }
    if (signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        relay_log_errno("signal SIGPIPE");
        return -1;
    }
    return 0;
}

static int run_recover(const struct options *options) {
    struct switch_hardware hardware;
    int final_state = 0;
    int virtual_fd;
    int result = RELAY_EXIT_OK;

    /*
     * Read the hardware exactly once, *before* creating the virtual device.
     * A second read after UI_DEV_CREATE could observe the relay's own output,
     * which is exactly what the old recovery code did (it inverted the
     * repaired state).  A missing/unreadable/ambiguous switch is repaired to
     * OFF: GNOME must not keep a stale software tablet mode.
     */
    if (switch_read_hardware(options->switch_name, &hardware) != 0) {
        relay_log("recovery: cannot enumerate physical switches; emitting OFF");
    } else if (hardware.error != 0) {
        relay_log("recovery: physical switch unreadable; emitting OFF");
    } else if (hardware.present > 1) {
        relay_log("recovery: physical switch ambiguous; emitting OFF");
    } else if (hardware.present == 1 && hardware.value == 1) {
        final_state = 1;
    }

    virtual_fd = uinput_create_switch();
    if (virtual_fd < 0) {
        emit_released(RELAY_EXIT_UINPUT);
        return RELAY_EXIT_UINPUT;
    }
    /*
     * Emit a known ON transition first, then settle on the real final state.
     * A fresh uinput switch starts with SW_TABLET_MODE off; consumers are not
     * required to deliver a synthetic event for the initial state and may
     * coalesce an event whose value equals the state they already assume.
     * The 0.2.x recovery relied on an unconditional ON→final transition
     * exactly for this reason: without it, repairing a stale software-ON
     * residue when the physical switch is OFF/absent (final OFF) could be a
     * no-op on the GNOME side.  ON→ON (physical folded) is a harmless
     * duplicate; ON→OFF forces the required visible transition.
     */
    if (uinput_emit_switch(virtual_fd, 1) < 0) {
        result = RELAY_EXIT_EMIT;
    } else {
        sleep_milliseconds(RELAY_RECOVER_TRANSITION_MILLISECONDS);
        if (uinput_emit_switch(virtual_fd, final_state) < 0) {
            result = RELAY_EXIT_EMIT;
        } else {
            sleep_milliseconds(RELAY_EVENT_DELAY_MILLISECONDS);
        }
    }
    uinput_destroy(virtual_fd);
    emit_released(result);
    return result;
}

static int run_relay(const struct options *options) {
    struct relay_state relay;
    struct switch_hardware hardware;
    int result = RELAY_EXIT_OK;
    int early;
    int final_state;

    relay_init(&relay, options);

    /* Hardware is enumerated before the virtual switch exists; every
     * discovery path also excludes the virtual device by identity. */
    if (switch_read_hardware(options->switch_name, &hardware) != 0 ||
        hardware.error != 0 || hardware.present > 1) {
        relay_log("physical tablet switch is unreadable or ambiguous");
        emit_released(RELAY_EXIT_HARDWARE);
        return RELAY_EXIT_HARDWARE;
    }
    relay.hardware_state =
            (hardware.present == 1 && hardware.value == 1) ? 1 : 0;

    if (install_signal_handlers() < 0) {
        emit_released(RELAY_EXIT_USAGE);
        return RELAY_EXIT_USAGE;
    }
    relay.virtual_fd = uinput_create_switch();
    if (relay.virtual_fd < 0) {
        emit_released(RELAY_EXIT_UINPUT);
        return RELAY_EXIT_UINPUT;
    }
    if (hardware.present == 1) {
        early = initial_attach(&relay);
        if (early != RELAY_EXIT_OK) {
            result = early;
            goto cleanup;
        }
    }

    relay.lease = monotonic_now() + RELAY_LEASE_SECONDS;
    relay.next_scan = monotonic_now();
    emit_ready(relay.physical != 0);

    while (!stopping) {
        double now = monotonic_now();
        int desired;

        if (relay.release_requested) {
            relay_log("release requested; shutting down");
            break;
        }
        if (now >= relay.lease) {
            relay_log("software lease expired; releasing");
            break;
        }
        if (now >= relay.next_scan) {
            relay.next_scan = now + RELAY_SCAN_SECONDS;
            result = maintain_switch(&relay);
            if (result != RELAY_EXIT_OK)
                break;
        }
        if (relay.monitor_fd >= 0) {
            int state = switch_read_state(relay.monitor_fd);
            if (state >= 0)
                relay.hardware_state = state;
        }
        maintain_input(&relay, now);

        desired = relay.hardware_state || relay.software;
        if (relay.output != desired) {
            if (uinput_emit_switch(relay.virtual_fd, desired) < 0) {
                result = RELAY_EXIT_EMIT;
                break;
            }
            relay.output = desired;
        }
        emit_if_changed(&relay, now);

        result = poll_events(&relay);
        if (result != RELAY_EXIT_OK)
            break;
    }

cleanup:
    final_state = relay.hardware_state;
    input_release(&relay.inputs);
    release_monitor(&relay, 0);
    if (relay.virtual_fd >= 0) {
        /* Leave the virtual switch in the physical state so GNOME does not
         * keep the software tablet mode after the device disappears. */
        if (relay.output != final_state &&
            uinput_emit_switch(relay.virtual_fd, final_state) < 0 &&
            result == RELAY_EXIT_OK)
            result = RELAY_EXIT_EMIT;
        sleep_milliseconds(RELAY_EVENT_DELAY_MILLISECONDS);
        uinput_destroy(relay.virtual_fd);
        relay.virtual_fd = -1;
    }
    emit_released(result);
    return result;
}

int main(int argc, char **argv) {
    struct options options;

    setbuf(stdout, NULL);
    if (parse_options(argc, argv, &options) < 0)
        return RELAY_EXIT_USAGE;
    if (geteuid() != 0) {
        relay_log("must run as root");
        return RELAY_EXIT_USAGE;
    }
    if (signal(SIGPIPE, SIG_IGN) == SIG_ERR) {
        relay_log_errno("signal SIGPIPE");
        return RELAY_EXIT_USAGE;
    }
    if (prctl(PR_SET_PDEATHSIG, SIGTERM) < 0)
        relay_log_errno("prctl(PR_SET_PDEATHSIG)");

    if (options.recover)
        return run_recover(&options);
    return run_relay(&options);
}
