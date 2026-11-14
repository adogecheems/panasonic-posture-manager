/* Discovery and contact tests replace every device operation with fakes. */
#include "../common.h"
#include <assert.h>
#include <glob.h>

struct fake_panel {
    int direct, contact, keyboard, own, readable, identity;
};

static struct fake_panel panels[2];
static int panel_count;
static int fd_panel[1024];
static char *fake_paths[] = {"panel0", "panel1"};
static int fake_open(const char *path);
static int fake_identity(int fd, const char *path);
static int fake_ioctl(int fd, unsigned long request, ...);
static int fake_glob(const char *pattern, int flags,
                     int (*error)(const char *, int), glob_t *paths);
static void fake_globfree(glob_t *paths);

#define relay_open_event_device fake_open
#define relay_identity_matches_sysfs fake_identity
#define ioctl fake_ioctl
#define glob fake_glob
#define globfree fake_globfree
#include "../input.c"
#undef relay_open_event_device
#undef relay_identity_matches_sysfs
#undef ioctl
#undef glob
#undef globfree

static int fake_open(const char *path) {
    int index = strcmp(path, "panel0") == 0 ? 0 : 1;
    int fd;

    if (!panels[index].readable)
        return -1;
    fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
    assert(fd >= 0 && fd < 1024);
    fd_panel[fd] = index;
    return fd;
}

static int fake_identity(int fd, const char *path) {
    (void)path;
    return panels[fd_panel[fd]].identity;
}

static void set_bit(unsigned char *bits, int bit) {
    bits[bit / 8] |= (unsigned char)(1u << (bit % 8));
}

static int fake_ioctl(int fd, unsigned long request, ...) {
    struct fake_panel *panel = &panels[fd_panel[fd]];
    va_list args;
    void *result;
    size_t size = _IOC_SIZE(request);

    va_start(args, request);
    result = va_arg(args, void *);
    va_end(args);
    memset(result, 0, size);
    switch (_IOC_NR(request)) {
    case 0x06: /* EVIOCGNAME */
        snprintf(result, size, "%s", panel->own ? WAKE_NAME : "Touchscreen");
        break;
    case 0x07: /* EVIOCGPHYS */
        snprintf(result, size, "panel%d", fd_panel[fd]);
        break;
    case 0x02: /* EVIOCGID */
        ((struct input_id *)result)->bustype = BUS_USB;
        break;
    case 0x09: /* EVIOCGPROP */
        if (panel->direct)
            set_bit(result, INPUT_PROP_DIRECT);
        break;
    case 0x21: /* EVIOCGBIT(EV_KEY) */
        if (panel->contact)
            set_bit(result, BTN_TOUCH);
        if (panel->keyboard)
            set_bit(result, KEY_A);
        break;
    case 0x23: /* EVIOCGBIT(EV_ABS) */
        set_bit(result, ABS_MT_POSITION_X);
        set_bit(result, ABS_MT_POSITION_Y);
        break;
    default:
        return -1;
    }
    return 0;
}

static int fake_glob(const char *pattern, int flags,
                     int (*error)(const char *, int), glob_t *paths) {
    (void)pattern;
    (void)flags;
    (void)error;
    paths->gl_pathc = (size_t)panel_count;
    paths->gl_pathv = fake_paths;
    return panel_count ? 0 : GLOB_NOMATCH;
}

static void fake_globfree(glob_t *paths) {
    (void)paths;
}

static void test_discovery(void) {
    int fd;

    panels[0] = (struct fake_panel){1, 1, 0, 0, 1, 1};
    panel_count = 1;
    fd = input_refresh_touchscreen(-1);
    assert(fd >= 0);
    assert(input_refresh_touchscreen(fd) == fd);
    panels[1] = panels[0];
    panel_count = 2;
    assert(input_refresh_touchscreen(fd) == -1);
    assert(fcntl(fd, F_GETFD) == -1 && errno == EBADF);
    panels[1].readable = 0;
    assert(input_refresh_touchscreen(-1) == -1);
    panel_count = 1;
    panels[0].identity = 0;
    assert(input_refresh_touchscreen(-1) == -1);
    panels[0].identity = 1;
    panels[0].direct = 0;
    assert(input_refresh_touchscreen(-1) == -1);
    panels[0].direct = 1;
    panels[0].contact = 0;
    assert(input_refresh_touchscreen(-1) == -1);
    panels[0].contact = 1;
    panels[0].keyboard = 1;
    assert(input_refresh_touchscreen(-1) == -1);
    panels[0].keyboard = 0;
    panels[0].own = 1;
    assert(input_refresh_touchscreen(-1) == -1);
}

static void test_contact(void) {
    int fds[2], desynchronized = 0;
    struct input_event events[4] = {0};

    assert(pipe(fds) == 0);
    events[0].type = EV_ABS;
    events[0].code = ABS_MT_POSITION_X;
    events[0].value = 100;
    events[1].type = EV_KEY;
    events[1].code = BTN_TOUCH;
    events[1].value = 1;
    assert(write(fds[1], events, sizeof events) == sizeof events);
    assert(input_touch_activity(fds[0], &desynchronized) == 1);
    events[1].value = 0;
    assert(write(fds[1], events, sizeof events) == sizeof events);
    assert(input_touch_activity(fds[0], &desynchronized) == 0);
    events[0].type = EV_SYN;
    events[0].code = SYN_DROPPED;
    events[1].value = 1;
    assert(write(fds[1], events, sizeof events) == sizeof events);
    assert(input_touch_activity(fds[0], &desynchronized) == 0);
    events[0].code = SYN_REPORT;
    assert(write(fds[1], events, sizeof events) == sizeof events);
    assert(input_touch_activity(fds[0], &desynchronized) == 1);
    close(fds[1]);
    assert(input_touch_activity(fds[0], &desynchronized) == -1);
    close(fds[0]);
}

int main(void) {
    test_discovery();
    test_contact();
    puts("touch input C tests passed");
    return 0;
}
