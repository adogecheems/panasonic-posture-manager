/*
 * Host-side tests for the relay's protocol, option parsing and pure helpers.
 *
 * relay.c is included into this translation unit so its static protocol
 * emitters can be tested without opening /dev/uinput or /dev/input.  Device
 * grabbing remains covered by the manual real-hardware checks.
 */
#define main relay_program_main
#include "../relay.c"
#undef main

#include <assert.h>

static int failures;

#define CHECK(condition)                                                       \
    do {                                                                       \
        if (!(condition)) {                                                    \
            fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__,           \
                    #condition);                                               \
            failures++;                                                        \
        }                                                                      \
    } while (0)

static int count_occurrences(const char *text, const char *needle) {
    int count = 0;
    size_t length = strlen(needle);

    while ((text = strstr(text, needle)) != NULL) {
        count++;
        text += length;
    }
    return count;
}

static void test_event_index(void) {
    int index = -1;

    CHECK(relay_event_index("/dev/input/event17", &index) == 0);
    CHECK(index == 17);
    CHECK(relay_event_index("event0", &index) == 0);
    CHECK(index == 0);
    CHECK(relay_event_index("/dev/input/event", &index) < 0);
    CHECK(relay_event_index("/dev/input/event-1", &index) < 0);
    CHECK(relay_event_index("/dev/input/event12x", &index) < 0);
    CHECK(relay_event_index("/dev/input/mouse0", &index) < 0);
    CHECK(relay_event_index(NULL, &index) < 0);
    CHECK(relay_event_index("/dev/input/event1000001", &index) < 0);
}

static void test_virtual_identity(void) {
    CHECK(uinput_is_own(VIRTUAL_NAME, "") == 1);
    CHECK(uinput_is_own("", VIRTUAL_PHYS) == 1);
    CHECK(uinput_is_own(WAKE_NAME, "") == 1);
    CHECK(uinput_is_own("", WAKE_PHYS) == 1);
    CHECK(uinput_is_own("Intel HID switches", "PNP0C50/input0") == 0);
    CHECK(uinput_is_own(NULL, NULL) == 0);
}

static void test_option_parsing(void) {
    struct options options;
    char *valid[] = {
        "relay",
        "--switch-name", "Intel HID switches",
        "--keyboard-name", "AT Translated Set 2 keyboard",
        "--touchpad-name", "Synaptics TM3562-001",
        "--recover",
    };
    char *unknown[] = {"relay", "--no-such-option"};
    char *missing[] = {"relay", "--switch-name"};

    CHECK(parse_options(8, valid, &options) == 0);
    CHECK(strcmp(options.switch_name, "Intel HID switches") == 0);
    CHECK(strcmp(options.keyboard_name, "AT Translated Set 2 keyboard") == 0);
    CHECK(strcmp(options.touchpad_name, "Synaptics TM3562-001") == 0);
    CHECK(options.recover == 1);
    CHECK(parse_options(2, unknown, &options) < 0);
    CHECK(parse_options(2, missing, &options) < 0);
}

static void begin_capture(char *path, size_t size, int *saved_stdout) {
    int descriptor;

    snprintf(path, size, "/tmp/ppm-relay-test-%ld-%d.out", (long)getpid(),
             rand());
    fflush(stdout);
    *saved_stdout = dup(STDOUT_FILENO);
    assert(*saved_stdout >= 0);
    descriptor = open(path, O_CREAT | O_TRUNC | O_WRONLY, 0600);
    assert(descriptor >= 0);
    assert(dup2(descriptor, STDOUT_FILENO) >= 0);
    close(descriptor);
}

static void end_capture(const char *path, int saved_stdout, char *buffer,
                        size_t size) {
    int descriptor;
    ssize_t length;

    fflush(stdout);
    assert(dup2(saved_stdout, STDOUT_FILENO) >= 0);
    close(saved_stdout);
    descriptor = open(path, O_RDONLY);
    assert(descriptor >= 0);
    length = read(descriptor, buffer, size - 1);
    assert(length >= 0);
    buffer[length] = '\0';
    close(descriptor);
    unlink(path);
}

static void test_protocol_messages(void) {
    struct relay_state relay;
    struct options options;
    char path[128];
    char output[1024];
    int saved;

    memset(&options, 0, sizeof options);
    relay_init(&relay, &options);

    begin_capture(path, sizeof path, &saved);
    emit_ready(1);
    emit_physical(0);
    emit_released(RELAY_EXIT_UINPUT);
    relay.hardware_state = 1;
    relay.software = 0;
    relay.physical = 1;
    emit_applied(&relay);
    end_capture(path, saved, output, sizeof output);

    CHECK(strstr(output,
                 "{\"protocol\":4,\"kind\":\"ready\",\"physical\":true}\n") !=
          NULL);
    CHECK(strstr(output,
                 "{\"protocol\":4,\"kind\":\"physical\",\"present\":false}\n") !=
          NULL);
    CHECK(strstr(output,
                 "{\"protocol\":4,\"kind\":\"released\",\"exit\":4}\n") != NULL);
    CHECK(strstr(output,
                 "{\"protocol\":4,\"kind\":\"applied\",\"hardware\":true,"
                 "\"tablet\":true,\"input_disabled\":false,"
                 "\"physical\":true,\"error\":null,\"request\":0,\"touch_serial\":0}\n") != NULL);

    relay.software = 1;
    relay.input_error = RELAY_ERROR_INPUT_GRAB;
    begin_capture(path, sizeof path, &saved);
    emit_applied(&relay);
    end_capture(path, saved, output, sizeof output);
    CHECK(strstr(output, "\"error\":\"input-grab-failed\"") != NULL);

    /* A stale grab error must never be emitted once both inputs are held. */
    relay.inputs.fds[0] = 0;
    relay.inputs.fds[1] = 0;
    begin_capture(path, sizeof path, &saved);
    emit_applied(&relay);
    end_capture(path, saved, output, sizeof output);
    CHECK(strstr(output, "\"input_disabled\":true") != NULL);
    CHECK(strstr(output, "\"error\":null") != NULL);
}

static void test_report_rate_limiting(void) {
    struct relay_state relay;
    struct options options;
    char path[128];
    char output[2048];
    int saved;

    memset(&options, 0, sizeof options);
    relay_init(&relay, &options);
    relay.hardware_state = 0;
    relay.software = 1;
    relay.physical = 0;

    begin_capture(path, sizeof path, &saved);
    emit_if_changed(&relay, 10.0);
    emit_if_changed(&relay, 10.1);
    emit_if_changed(&relay, 10.99);
    emit_if_changed(&relay, 11.01);
    end_capture(path, saved, output, sizeof output);

    /* First report plus one after the one-second interval, not four. */
    CHECK(count_occurrences(output, "\"kind\":\"applied\"") == 2);
}

static void test_hardware_handoff(void) {
    struct relay_state relay;
    struct options options = {0};
    char path[128];
    char output[512];
    int saved;

    relay_init(&relay, &options);
    relay.monitor = 1;
    relay.lease = 20.0;
    /* The physical device can first appear while software tent is active. */
    relay.physical = 1;
    update_handoff(&relay, 9.0);
    CHECK(relay_tablet_state(&relay) == 0); /* OFF alone cannot arm a hold. */
    relay.hardware_state = 1;
    update_handoff(&relay, 10.0);
    CHECK(relay_tablet_state(&relay) == 1);
    CHECK(relay_input_disabled(&relay) == 0);
    relay.hardware_state = 0;
    update_handoff(&relay, 11.0);
    CHECK(relay.hardware_state == 0);
    CHECK(relay_tablet_state(&relay) == 1);
    CHECK(relay.handoff_until == 13.0);
    begin_capture(path, sizeof path, &saved);
    emit_applied(&relay);
    end_capture(path, saved, output, sizeof output);
    CHECK(strstr(output, "\"hardware\":false,\"tablet\":true,"
                        "\"input_disabled\":false") != NULL);
    update_handoff(&relay, 12.9);
    CHECK(relay.handoff_until == 13.0);
    CHECK(relay_tablet_state(&relay) == 1);
    update_handoff(&relay, 13.0);
    CHECK(relay_tablet_state(&relay) == 0);
    update_handoff(&relay, 14.0);
    CHECK(relay_tablet_state(&relay) == 0); /* Heartbeats cannot rearm it. */

    relay.hardware_state = 1;
    update_handoff(&relay, 14.1);
    relay.hardware_state = 0;
    update_handoff(&relay, 14.2);
    relay.software = 1;
    relay.monitor = 0;
    update_handoff(&relay, 14.3);
    CHECK(relay_tablet_state(&relay) == 1); /* Stable tent: no intermediate OFF. */
    CHECK(relay.handoff_active == 0);

    relay.software = 0;
    relay.monitor = 1;
    relay.hardware_state = 1;
    update_handoff(&relay, 15.0);
    relay.hardware_state = 0;
    update_handoff(&relay, 15.1);
    relay.physical = 0;
    update_handoff(&relay, 15.2);
    CHECK(relay_tablet_state(&relay) == 0);

    relay.physical = 1;
    relay.hardware_state = 1;
    update_handoff(&relay, 16.0);
    relay.hardware_state = 0;
    update_handoff(&relay, 16.1);
    relay.lease = 16.2;
    update_handoff(&relay, 16.2);
    CHECK(relay_tablet_state(&relay) == 0);
}

static void test_monitor_commands_and_input_release(void) {
    struct relay_state relay;
    struct options options = {0};
    int commands[2];
    int inputs[2];
    int saved_stdin = dup(STDIN_FILENO);
    const char *sequence = "1001";
    unsigned int expected[] = {1, 2, 2, 3};
    size_t i;

    CHECK(saved_stdin >= 0);
    CHECK(pipe(commands) == 0);
    CHECK(dup2(commands[0], STDIN_FILENO) >= 0);
    close(commands[0]);
    relay_init(&relay, &options);
    relay.next_scan = 100.0;
    for (i = 0; i < 4; i++) {
        CHECK(write(commands[1], sequence + i, 1) == 1);
        CHECK(handle_stdin(&relay, POLLIN) == RELAY_EXIT_OK);
        CHECK(relay.request == expected[i]);
        CHECK(relay.software == (sequence[i] == '1'));
        CHECK(relay.monitor == (sequence[i] == '0'));
    }
    CHECK(relay.next_scan == 0.0); /* First hardware command forces discovery. */
    {
        double lease = relay.lease;
        unsigned int request = relay.request;
        CHECK(write(commands[1], "w", 1) == 1);
        CHECK(handle_stdin(&relay, POLLIN) == RELAY_EXIT_OK);
        CHECK(relay.lease == lease);
        CHECK(relay.request == request);
    }
    close(commands[1]);
    CHECK(handle_stdin(&relay, POLLIN) == RELAY_EXIT_OK);
    CHECK(relay.release_requested == 1);
    CHECK(dup2(saved_stdin, STDIN_FILENO) >= 0);
    close(saved_stdin);

    /* Pipe fds stand in for held inputs: no real devices are opened. */
    CHECK(pipe(inputs) == 0);
    relay.inputs.fds[0] = inputs[0];
    relay.inputs.fds[1] = inputs[1];
    relay.hardware_state = 0;
    relay.software = 0;
    relay.handoff_active = 1;
    relay.next_input = 100.0;
    maintain_input(&relay, 10.0);
    CHECK(!input_ready(&relay.inputs));
    CHECK(relay.next_input == 0.0);
    CHECK(relay_tablet_state(&relay) == 1);
}

static void test_cleanup_state(void) {
    struct switch_hardware hardware;

    memset(&hardware, 0, sizeof hardware);
    hardware.present = 1;
    hardware.value = 1;
    hardware.error = 0;
    CHECK(relay_cleanup_state(0, 0, &hardware) == 1);

    hardware.value = 0;
    CHECK(relay_cleanup_state(1, 0, &hardware) == 0);

    hardware.present = 0;
    hardware.value = -1;
    CHECK(relay_cleanup_state(1, 0, &hardware) == 0);

    hardware.present = 2;
    hardware.value = 1;
    CHECK(relay_cleanup_state(1, 0, &hardware) == 0);

    hardware.present = 1;
    hardware.error = 1;
    CHECK(relay_cleanup_state(1, 0, &hardware) == 0);

    CHECK(relay_cleanup_state(1, -1, &hardware) == 1);
    CHECK(relay_cleanup_state(-1, -1, &hardware) == 0);
    CHECK(relay_cleanup_state(1, 0, NULL) == 0);
}

static void test_contact_backed_wake(void) {
    struct relay_state relay;
    struct options options = {0};
    struct input_event events[4];
    int output[2];

    relay_init(&relay, &options);
    CHECK(pipe(output) == 0);
    relay.software = 1;
    relay.inputs.fds[0] = relay.inputs.fds[1] = 0;
    relay.touch_fd = 0;
    relay.wake_fd = output[1];
    relay.lease = 20.0;
    relay.last_touch = 10.0;
    relay.touch_serial = 9;
    CHECK(relay_can_wake(&relay, 11.0));
    CHECK(!relay_can_wake(&relay, 9.0));
    CHECK(!relay_can_wake(&relay, 12.01));
    relay.lease = 11.0;
    CHECK(!relay_can_wake(&relay, 11.0));
    relay.lease = 20.0;
    relay.hardware_state = 1;
    CHECK(!relay_can_wake(&relay, 11.0));
    relay.hardware_state = 0;
    relay.software = 0;
    CHECK(!relay_can_wake(&relay, 11.0));
    relay.software = 1;
    relay.inputs.fds[1] = -1;
    CHECK(!relay_can_wake(&relay, 11.0));
    relay.inputs.fds[1] = 0;
    request_wake(&relay, 11.0);
    CHECK(!relay_can_wake(&relay, 11.0));
    CHECK(relay.lease == 20.0);
    CHECK(relay.request == 0);
    CHECK(read(output[0], events, sizeof events) == sizeof events);
    CHECK(events[0].type == EV_KEY && events[0].code == KEY_WAKEUP && events[0].value == 1);
    CHECK(events[1].type == EV_SYN && events[1].code == SYN_REPORT);
    CHECK(events[2].type == EV_KEY && events[2].code == KEY_WAKEUP && events[2].value == 0);
    CHECK(events[3].type == EV_SYN && events[3].code == SYN_REPORT);
    close(output[0]);
    close(output[1]);
}

int main(void) {
    setbuf(stdout, NULL);
    srand((unsigned)getpid());
    test_event_index();
    test_virtual_identity();
    test_option_parsing();
    test_protocol_messages();
    test_report_rate_limiting();
    test_cleanup_state();
    test_hardware_handoff();
    test_monitor_commands_and_input_release();
    test_contact_backed_wake();
    if (failures != 0) {
        fprintf(stderr, "%d relay test(s) failed\n", failures);
        return 1;
    }
    puts("relay C tests passed");
    return 0;
}
