#include "native.h"
#include <ctype.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

int64_t now_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_REALTIME, &t);
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
int service_lock(const char *home, const char *name) {
    char path[1200];
    snprintf(path, sizeof(path), "%s/%s.lock", home, name);
    int fd = open(path, O_RDWR | O_CREAT | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0)
        return -1;
    if (flock(fd, LOCK_EX | LOCK_NB)) {
        close(fd);
        return -1;
    }
    return fd;
}
int64_t monotonic_ms(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return (int64_t)t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
int atomic_write(const char *path, const char *data) {
    char tmp[1200], dir[1200];
    if (snprintf(tmp, sizeof(tmp), "%s.tmp.%ld", path, (long)getpid()) >= (int)sizeof(tmp))
        return -1;
    int fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (fd < 0)
        return -1;
    size_t left = strlen(data);
    const char *at = data;
    while (left) {
        ssize_t n = write(fd, at, left);
        if (n <= 0) {
            close(fd);
            unlink(tmp);
            return -1;
        }
        at += n;
        left -= (size_t)n;
    }
    int rc = fsync(fd);
    close(fd);
    if (rc || rename(tmp, path)) {
        unlink(tmp);
        return -1;
    }
    snprintf(dir, sizeof(dir), "%s", path);
    char *slash = strrchr(dir, '/');
    if (slash) {
        *slash = 0;
        fd = open(*dir ? dir : "/", O_RDONLY | O_DIRECTORY);
        if (fd < 0)
            return -1;
        rc = fsync(fd);
        close(fd);
    }
    return rc;
}
static int safe(const char *s) {
    for (; *s; s++)
        if ((unsigned char)*s < 32 || (unsigned char)*s == 127)
            return 0;
    return 1;
}
int config_valid(const Config *c) {
    if (!safe(c->ssid) || !safe(c->password) || !safe(c->url) || !safe(c->token) ||
        !safe(c->device_id))
        return 0;
    if (strlen(c->ssid) > 32)
        return 0;
    size_t n = strlen(c->password);
    if (n && (n < 8 || n > 63)) {
        if (n != 64)
            return 0;
        for (size_t i = 0; i < n; i++)
            if (!isxdigit((unsigned char)c->password[i]))
                return 0;
    }
    if (!*c->device_id)
        return 0;
    /* Empty destination is allowed during setup; upload requires URL and token. */
    if (*c->url && strncmp(c->url, "https://", 8) && strncmp(c->url, "http://", 7))
        return 0;
    if (strpbrk(c->url, " \"\\") || strpbrk(c->token, " \"\\"))
        return 0;
    return 1;
}
int config_load(const char *home, Config *c) {
    memset(c, 0, sizeof(*c));
    strcpy(c->device_id, "nokia-8110");
    char path[1200], line[1600];
    snprintf(path, sizeof(path), "%s/config", home);
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    while (fgets(line, sizeof(line), f)) {
        if (!strchr(line, '\n') && !feof(f)) {
            fclose(f);
            return -1;
        }
        line[strcspn(line, "\r\n")] = 0;
        if (!*line || *line == '#')
            continue;
        char *v = strchr(line, '=');
        if (!v) {
            fclose(f);
            return -1;
        }
        *v++ = 0;
        char *dest = NULL;
        size_t cap = 0;
#define FIELD(n)                                                                                   \
    if (!strcmp(line, #n)) {                                                                       \
        dest = c->n;                                                                               \
        cap = sizeof(c->n);                                                                        \
    }
        FIELD(ssid)
        else FIELD(password) else FIELD(url) else FIELD(token) else FIELD(device_id)
#undef FIELD
            if (!dest || strlen(v) >= cap) {
            fclose(f);
            return -1;
        }
        strcpy(dest, v);
    }
    int failed = ferror(f);
    fclose(f);
    return !failed && config_valid(c) ? 0 : -1;
}
int config_save(const char *home, const Config *c) {
    if (!config_valid(c))
        return -1;
    char path[1200], buf[2200];
    snprintf(path, sizeof(path), "%s/config", home);
    snprintf(buf, sizeof(buf), "ssid=%s\npassword=%s\nurl=%s\ntoken=%s\ndevice_id=%s\n", c->ssid,
             c->password, c->url, c->token, c->device_id);
    return atomic_write(path, buf);
}
int json_string(char *out, size_t cap, const char *s) {
    size_t n = 0;
    if (cap < 3)
        return -1;
    out[n++] = '"';
    for (; *s; s++) {
        unsigned char ch = (unsigned char)*s;
        if (n + 7 >= cap)
            return -1;
        if (ch == '"' || ch == '\\') {
            out[n++] = '\\';
            out[n++] = (char)ch;
        } else if (ch < 32) {
            snprintf(out + n, 7, "\\u%04x", ch);
            n += 6;
        } else
            out[n++] = (char)ch;
    }
    out[n++] = '"';
    out[n] = 0;
    return (int)n;
}
static const char *ws(const char *s) {
    while (isspace((unsigned char)*s))
        s++;
    return s;
}
static int literal(const char **p, const char *value) {
    *p = ws(*p);
    size_t n = strlen(value);
    if (strncmp(*p, value, n))
        return 0;
    *p += n;
    return 1;
}
int ack_valid(const char *json, const char *key) {
    char quoted[160];
    if (json_string(quoted, sizeof(quoted), key) < 0)
        return 0;
    const char *p = json;
    return literal(&p, "{") && literal(&p, "\"accepted\"") && literal(&p, ":") &&
           literal(&p, "[") && literal(&p, quoted) && literal(&p, "]") && literal(&p, "}") &&
           !*ws(p);
}
