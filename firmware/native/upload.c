#include "native.h"
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t sync_now = 0;
static void request_sync(int sig) {
    (void)sig;
    sync_now = 1;
}
static int upload_one(const char *home, const char *curl, sqlite3 *db, const Config *c) {
    char key[128], request[1200], response[1200], cfgpath[1200], cfg[6000], ca[1200];
    int64_t id;
    char *json = malloc(400000);
    if (!json)
        return -1;
    int rc = store_next(db, c, json, 400000, key, sizeof(key), &id);
    if (rc != 1) {
        free(json);
        return rc;
    }
    snprintf(request, sizeof(request), "%s/request.json", home);
    snprintf(response, sizeof(response), "%s/response.json", home);
    snprintf(cfgpath, sizeof(cfgpath), "%s/curl.conf", home);
    snprintf(ca, sizeof(ca), "%s/ca.pem", home);
    if (atomic_write(request, json)) {
        free(json);
        return -1;
    }
    free(json);
    if (atomic_write(response, ""))
        return -1;
    /* Config validation rejects newlines/quotes in credentials and URL; never use a shell. */
    const char *proto = !strncmp(c->url, "https://", 8) ? "https" : "http";
    int n =
        snprintf(cfg, sizeof(cfg),
                 "url = \"%s\"\nheader = \"Authorization: Bearer %s\"\nheader = \"Content-Type: "
                 "application/json\"\ndata-binary = \"@%s\"\noutput = \"%s\"\nconnect-timeout = "
                 "10\nmax-time = 30\nmax-filesize = 8192\nfail\nsilent\nproto = \"=%s\"\n",
                 c->url, c->token, request, response, proto);
    if (!strcmp(proto, "https")) {
        if (access(ca, R_OK))
            snprintf(ca, sizeof(ca), "/system/kaiforward/ca.pem");
        if (access(ca, R_OK))
            return -1;
        n += snprintf(cfg + n, sizeof(cfg) - (size_t)n, "cacert = \"%s\"\n", ca);
    }
    if (n < 0 || (size_t)n >= sizeof(cfg) || atomic_write(cfgpath, cfg))
        return -1;
    pid_t child = fork();
    if (child < 0)
        return -1;
    if (!child) {
        int null = open("/dev/null", O_RDWR);
        if (null >= 0) {
            dup2(null, STDOUT_FILENO);
            dup2(null, STDERR_FILENO);
            close(null);
        }
        execl(curl, curl, "--disable", "--config", cfgpath, (char *)NULL);
        _exit(127);
    }
    int status = 0;
    int64_t deadline = monotonic_ms() + 35000;
    for (;;) {
        pid_t done = waitpid(child, &status, WNOHANG);
        if (done == child)
            break;
        if (done < 0 && errno != EINTR) {
            unlink(cfgpath);
            return -1;
        }
        if (monotonic_ms() > deadline) {
            kill(child, SIGKILL);
            while (waitpid(child, &status, 0) < 0 && errno == EINTR) {
            }
            unlink(cfgpath);
            unlink(request);
            return -1;
        }
        usleep(100000);
    }
    unlink(cfgpath);
    unlink(request);
    if (!WIFEXITED(status) || WEXITSTATUS(status))
        return -1;
    FILE *f = fopen(response, "r");
    if (!f)
        return -1;
    char reply[8193];
    size_t size = fread(reply, 1, 8192, f);
    int extra = fgetc(f), bad = ferror(f);
    fclose(f);
    unlink(response);
    reply[size] = 0;
    if (extra != EOF || bad || memchr(reply, 0, size) || !ack_valid(reply, key))
        return -1;
    return store_ack(db, id) ? -1 : 1;
}
void uploader(const char *home, const char *curl) {
    int lock = service_lock(home, "upload");
    if (lock < 0)
        _exit(1);
    signal(SIGUSR1, request_sync);
    sqlite3 *db = store_open(home);
    if (!db)
        _exit(1);
    int failures = 0;
    int64_t next = 0;
    char path[1200], status[200];
    snprintf(path, sizeof(path), "%s/upload.status", home);
    for (;;) {
        char syncpath[1200];
        snprintf(syncpath, sizeof(syncpath), "%s/sync", home);
        if (!access(syncpath, F_OK)) {
            unlink(syncpath);
            sync_now = 1;
        }
        if (sync_now || monotonic_ms() >= next) {
            sync_now = 0;
            Config c;
            int rc = -1;
            if (!config_load(home, &c) && *c.url && *c.token) {
                rc = 0;
                for (int batch = 0; batch < 50; batch++) {
                    rc = upload_one(home, curl, db, &c);
                    if (rc <= 0)
                        break;
                }
            }
            if (rc >= 0) {
                failures = 0;
                next = monotonic_ms() + (rc == 1 ? 1000 : 60000);
                snprintf(status, sizeof(status), "%lld\n%s\n", (long long)now_ms(),
                         rc == 1 ? "SYNCING" : "QUEUE EMPTY");
            } else {
                if (failures < 5)
                    failures++;
                int delay = 10 * (1 << failures);
                if (delay > 300)
                    delay = 300;
                next = monotonic_ms() + delay * 1000;
                snprintf(status, sizeof(status), "%lld\nRETRYING / CHECK SETUP\n",
                         (long long)now_ms());
            }
            atomic_write(path, status);
        }
        usleep(250000);
    }
}
