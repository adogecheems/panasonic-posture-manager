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
                 "{\"protocol\":2,\"kind\":\"ready\",\"physical\":true}\n") !=
          NULL);
    CHECK(strstr(output,
                 "{\"protocol\":2,\"kind\":\"physical\",\"present\":false}\n") !=
          NULL);
    CHECK(strstr(output,
                 "{\"protocol\":2,\"kind\":\"released\",\"exit\":4}\n") != NULL);
    CHECK(strstr(output,
                 "{\"protocol\":2,\"kind\":\"applied\",\"hardware\":true,"
                 "\"tablet\":true,\"input_disabled\":false,"
                 "\"physical\":true,\"error\":null}\n") != NULL);

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

int main(void) {
    setbuf(stdout, NULL);
    srand((unsigned)getpid());
    test_event_index();
    test_virtual_identity();
    test_option_parsing();
    test_protocol_messages();
    test_report_rate_limiting();
    if (failures != 0) {
        fprintf(stderr, "%d relay test(s) failed\n", failures);
        return 1;
    }
    puts("relay C tests passed");
    return 0;
}
