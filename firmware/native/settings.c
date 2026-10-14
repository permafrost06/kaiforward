#include "native.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define SETTINGS_LIMIT 16384

static int load(const char *home, Config *c) {
    if (!config_load(home, c))
        return 0;
    char path[1200];
    snprintf(path, sizeof(path), "%s/config", home);
    /* config_load supplies defaults for an unprovisioned phone. */
    return access(path, F_OK) && errno == ENOENT ? 0 : -1;
}

int settings_read(const char *home, char *json, size_t cap) {
    Config c;
    char ssid[800], url[6200], relay_url[1600];
    if (load(home, &c) || json_string(ssid, sizeof(ssid), c.ssid) < 0 ||
        json_string(url, sizeof(url), c.url) < 0 ||
        json_string(relay_url, sizeof(relay_url), c.relay_url) < 0)
        return -1;
    int n = snprintf(json, cap,
                     "{\"ssid\":%s,\"url\":%s,\"relay_url\":%s,\"password_set\":%"
                     "s,\"token_set\":%s}\n",
                     ssid, url, relay_url, *c.password ? "true" : "false",
                     *c.token ? "true" : "false");
    return n < 0 || (size_t)n >= cap ? -1 : 0;
}

/* 0 success, 400 invalid patch, 409 busy, 500 storage failure. */
int settings_update(sqlite3 *db, const char *home, const char *json) {
    sqlite3_stmt *q = NULL;
    if (strlen(json) > SETTINGS_LIMIT)
        return 400;
    if (sqlite3_prepare_v2(db,
                           "SELECT json_valid(?1), CASE WHEN json_valid(?1) "
                           "THEN json_type(?1) END",
                           -1, &q, NULL) != SQLITE_OK)
        return 500;
    sqlite3_bind_text(q, 1, json, -1, SQLITE_TRANSIENT);
    int valid = sqlite3_step(q) == SQLITE_ROW && sqlite3_column_int(q, 0) &&
                !strcmp((const char *)sqlite3_column_text(q, 1), "object");
    sqlite3_finalize(q);
    q = NULL;
    if (!valid)
        return 400;
    int lock = service_lock(home, "settings");
    if (lock < 0)
        return 409;
    Config c;
    int result = 500;
    if (load(home, &c))
        goto done;
    if (sqlite3_prepare_v2(db, "SELECT key, type, value FROM json_each(?1)", -1, &q, NULL) !=
        SQLITE_OK)
        goto done;
    sqlite3_bind_text(q, 1, json, -1, SQLITE_TRANSIENT);
    unsigned seen = 0;
    int rc;
    result = 400;
    while ((rc = sqlite3_step(q)) == SQLITE_ROW) {
        const char *name = (const char *)sqlite3_column_text(q, 0);
        const char *type = (const char *)sqlite3_column_text(q, 1);
        const char *value = (const char *)sqlite3_column_text(q, 2);
        if ((size_t)sqlite3_column_bytes(q, 0) != strlen(name))
            goto done;
        char *dest = NULL;
        size_t cap = 0;
        unsigned bit = 0;
        struct {
            const char *name;
            char *value;
            size_t cap;
        } fields[] = {{"ssid", c.ssid, sizeof(c.ssid)},
                      {"password", c.password, sizeof(c.password)},
                      {"url", c.url, sizeof(c.url)},
                      {"token", c.token, sizeof(c.token)},
                      {"relay_url", c.relay_url, sizeof(c.relay_url)}};
        for (unsigned i = 0; i < sizeof(fields) / sizeof(fields[0]); i++)
            if (!strcmp(name, fields[i].name)) {
                dest = fields[i].value;
                cap = fields[i].cap;
                bit = 1u << i;
            }
        int bytes = sqlite3_column_bytes(q, 2);
        if (!dest || strcmp(type, "text") || (seen & bit) || bytes < 0 || (size_t)bytes >= cap ||
            memchr(value, 0, (size_t)bytes))
            goto done;
        seen |= bit;
        memcpy(dest, value, (size_t)bytes);
        dest[bytes] = 0;
    }
    if (rc != SQLITE_DONE) {
        result = 500;
        goto done;
    }
    if (!seen || !config_valid(&c))
        goto done;
    sqlite3_finalize(q);
    q = NULL;
    result = 500;
    if (config_save(home, &c))
        goto done;
    char path[1200];
    snprintf(path, sizeof(path), "%s/reload", home);
    if (atomic_write(path, ""))
        goto done;
    snprintf(path, sizeof(path), "%s/sync", home);
    if (atomic_write(path, ""))
        goto done;
    result = 0;
done:
    sqlite3_finalize(q);
    close(lock);
    return result;
}

int settings_key(const char *home, char key[65]) {
    char path[1200];
    snprintf(path, sizeof(path), "%s/settings.key", home);
    int lock = service_lock(home, "settings-key");
    if (lock < 0)
        return -1;
    int rc = -1;
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd >= 0) {
        char stored[66];
        ssize_t n = read(fd, stored, sizeof(stored));
        close(fd);
        if (n != 65 || stored[64] != '\n')
            goto done;
        for (int i = 0; i < 64; i++)
            if (!strchr("0123456789abcdef", stored[i]) || !stored[i])
                goto done;
        memcpy(key, stored, 64);
        key[64] = 0;
        rc = 0;
    } else if (errno == ENOENT) {
        unsigned char random[32];
        fd = open("/dev/urandom", O_RDONLY | O_CLOEXEC);
        if (fd < 0)
            goto done;
        size_t used = 0;
        while (used < sizeof(random)) {
            ssize_t n = read(fd, random + used, sizeof(random) - used);
            if (n < 0 && errno == EINTR)
                continue;
            if (n <= 0)
                break;
            used += (size_t)n;
        }
        close(fd);
        if (used != sizeof(random))
            goto done;
        char stored[66];
        for (size_t i = 0; i < sizeof(random); i++)
            snprintf(stored + i * 2, 3, "%02x", random[i]);
        stored[64] = '\n';
        stored[65] = 0;
        if (atomic_write(path, stored))
            goto done;
        memcpy(key, stored, 64);
        key[64] = 0;
        rc = 0;
    }
done:
    close(lock);
    return rc;
}

/* Actions: 1 redacted read, 2 JSON patch on stdin, 3 HTTP access key. */
int settings_command(const char *home, int action) {
    char json[SETTINGS_LIMIT + 1];
    if (action == 3) {
        char key[65];
        if (settings_key(home, key))
            goto failed;
        puts(key);
        return 0;
    }
    if (action == 2) {
        size_t used = fread(json, 1, sizeof(json) - 1, stdin);
        if (ferror(stdin) || fgetc(stdin) != EOF || memchr(json, 0, used)) {
            fprintf(stderr, "Invalid settings JSON or request too large\n");
            return 1;
        }
        json[used] = 0;
        sqlite3 *db = NULL;
        if (sqlite3_open(":memory:", &db) != SQLITE_OK) {
            sqlite3_close(db);
            goto failed;
        }
        int result = settings_update(db, home, json);
        sqlite3_close(db);
        if (result) {
            fprintf(stderr,
                    "Settings update failed (%d); use a JSON object with valid "
                    "settings strings\n",
                    result);
            return 1;
        }
    }
    if (settings_read(home, json, sizeof(json)))
        goto failed;
    fputs(json, stdout);
    return 0;
failed:
    fprintf(stderr, "Native settings storage unavailable\n");
    return 1;
}
