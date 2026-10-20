/* Host-test clock: advance native deadlines without sleeping for minutes. */
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

int clock_gettime(clockid_t id, struct timespec *out) {
    const char *path = getenv("KF_TEST_MONOTONIC_FILE");
    if (id == CLOCK_MONOTONIC && path) {
        int fd = open(path, O_RDONLY);
        if (fd >= 0) {
            char data[64];
            ssize_t n = read(fd, data, sizeof(data) - 1);
            close(fd);
            if (n > 0) {
                data[n] = 0;
                long long ms = strtoll(data, NULL, 10);
                out->tv_sec = ms / 1000;
                out->tv_nsec = (ms % 1000) * 1000000;
                return 0;
            }
        }
    }
    return syscall(SYS_clock_gettime, id, out);
}
