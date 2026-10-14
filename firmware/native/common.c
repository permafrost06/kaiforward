#include "native.h"
#include <arpa/inet.h>
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
    const unsigned char *p = (const unsigned char *)s;
    while (*p) {
        if (*p < 32 || *p == 127)
            return 0;
        if (*p < 128) {
            p++;
            continue;
        }
        unsigned count, code;
        if (*p >= 0xc2 && *p <= 0xdf) {
            count = 1;
            code = *p & 0x1f;
        } else if (*p >= 0xe0 && *p <= 0xef) {
            count = 2;
            code = *p & 0x0f;
        } else if (*p >= 0xf0 && *p <= 0xf4) {
            count = 3;
            code = *p & 7;
        } else
            return 0;
        p++;
        unsigned minimum = count == 1 ? 0x80 : count == 2 ? 0x800 : 0x10000;
        while (count--) {
            if ((*p & 0xc0) != 0x80)
                return 0;
            code = (code << 6) | (*p++ & 0x3f);
        }
        if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff))
            return 0;
    }
    return 1;
}
static int receiver_url_valid(const char *url) {
    if (!*url)
        return 1;
    const char *host = !strncmp(url, "https://", 8) ? url + 8 :
                       !strncmp(url, "http://", 7) ? url + 7 : NULL;
    if (!host || strchr(url, '#'))
        return 0;
    const char *end = host + strcspn(host, "/?");
    if (memchr(host, '@', (size_t)(end - host)))
        return 0;
    const char *port = NULL;
    if (*host == '[') {
        const char *close = memchr(host, ']', (size_t)(end - host));
        char ip[INET6_ADDRSTRLEN];
        struct in6_addr addr;
        if (!close || close - host <= 1 || (size_t)(close - host - 1) >= sizeof(ip))
            return 0;
        memcpy(ip, host + 1, (size_t)(close - host - 1));
        ip[close - host - 1] = 0;
        if (inet_pton(AF_INET6, ip, &addr) != 1)
            return 0;
        if (close + 1 != end) {
            if (close[1] != ':')
                return 0;
            port = close + 2;
        }
    } else {
        port = memchr(host, ':', (size_t)(end - host));
        if ((port ? port : end) == host || memchr(host, '[', (size_t)(end - host)) ||
            memchr(host, ']', (size_t)(end - host)))
            return 0;
        if (port)
            port++;
    }
    if (port) {
        if (port == end)
            return 0;
        unsigned n = 0;
        for (; port < end; port++) {
            if (!isdigit((unsigned char)*port))
                return 0;
            n = n * 10 + (unsigned)(*port - '0');
            if (n > 65535)
                return 0;
        }
        if (!n)
            return 0;
    }
    return 1;
}
int config_valid(const Config *c) {
    if (!safe(c->ssid) || !safe(c->password) || !safe(c->url) || !safe(c->token))
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
    /* Empty destination is allowed during setup; upload requires URL and token. */
    if (!receiver_url_valid(c->url))
        return 0;
    if (strpbrk(c->url, " \"\\") || strpbrk(c->token, " \"\\"))
        return 0;
    return 1;
}
int config_load(const char *home, Config *c) {
    memset(c, 0, sizeof(*c));
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
        /* Configs saved by earlier builds still carry the removed device ID. */
        if (!strcmp(line, "device_id"))
            continue;
        char *dest = NULL;
        size_t cap = 0;
#define FIELD(n)                                                                                   \
    if (!strcmp(line, #n)) {                                                                       \
        dest = c->n;                                                                               \
        cap = sizeof(c->n);                                                                        \
    }
        FIELD(ssid)
        else FIELD(password) else FIELD(url) else FIELD(token)
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
    snprintf(buf, sizeof(buf), "ssid=%s\npassword=%s\nurl=%s\ntoken=%s\n", c->ssid,
             c->password, c->url, c->token);
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
