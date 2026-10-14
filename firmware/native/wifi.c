#include "native.h"
#include <ctype.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

static int unescape(const char *s, char *out) {
    size_t n = 0;
    while (*s) {
        unsigned char ch = (unsigned char)*s++;
        if (ch == '\\') {
            if (*s == 'x' && isxdigit((unsigned char)s[1]) && isxdigit((unsigned char)s[2])) {
                char hex[3] = {s[1], s[2], 0};
                ch = (unsigned char)strtoul(hex, NULL, 16);
                s += 3;
            } else if (*s == '\\') {
                ch = (unsigned char)*s++;
            } else
                return -1;
        }
        /* Settings format cannot represent control characters. */
        if (ch < 32 || ch == 127 || n == 32)
            return -1;
        out[n++] = (char)ch;
    }
    out[n] = 0;
    return n ? 0 : -1;
}
int wifi_parse(char *reply, WifiNetwork *networks, int capacity) {
    int count = 0;
    char *save, *line = strtok_r(reply, "\n", &save);
    if (!line || strncmp(line, "bssid /", 7))
        return -1;
    while ((line = strtok_r(NULL, "\n", &save))) {
        char *fields[5] = {line};
        int ok = 1;
        for (int i = 1; i < 5; i++) {
            char *tab = strchr(fields[i - 1], '\t');
            if (!tab) {
                ok = 0;
                break;
            }
            *tab = 0;
            fields[i] = tab + 1;
        }
        if (!ok)
            continue;
        WifiNetwork entry = {0};
        char *end;
        long strength = strtol(fields[2], &end, 10);
        if (*end || strength < -200 || strength > 0 || unescape(fields[4], entry.ssid))
            continue;
        entry.signal = (int)strength;
        entry.security = strstr(fields[3], "PSK") ? 1
                         : (strstr(fields[3], "WEP") || strstr(fields[3], "EAP") ||
                            strstr(fields[3], "SAE") || strstr(fields[3], "OWE"))
                             ? 2
                             : 0;
        int at;
        for (at = 0; at < count; at++)
            if (!strcmp(networks[at].ssid, entry.ssid) && networks[at].security == entry.security)
                break;
        if (at < count) {
            if (networks[at].signal < entry.signal)
                networks[at] = entry;
        } else if (count < capacity)
            networks[count++] = entry;
        else if (capacity > 0) {
            int weakest = 0;
            for (int i = 1; i < count; i++)
                if (networks[i].signal < networks[weakest].signal)
                    weakest = i;
            if (entry.signal > networks[weakest].signal)
                networks[weakest] = entry;
        }
    }
    for (int i = 0; i < count; i++)
        for (int j = i + 1; j < count; j++)
            if (networks[j].signal > networks[i].signal) {
                WifiNetwork tmp = networks[i];
                networks[i] = networks[j];
                networks[j] = tmp;
            }
    return count;
}
static int control(int fd, const char *command, char *reply, size_t cap) {
    size_t len = strlen(command);
    if (send(fd, command, len, 0) != (ssize_t)len)
        return -1;
    ssize_t n;
    do {
        n = recv(fd, reply, cap - 1, 0);
    } while (n < 0 && errno == EINTR);
    if (n < 0)
        return -1;
    reply[n] = 0;
    return 0;
}
static void field(const char *reply, const char *name, char *out, size_t cap) {
    size_t n = strlen(name);
    *out = 0;
    while (*reply) {
        size_t len = strcspn(reply, "\r\n");
        if (len > n && !strncmp(reply, name, n) && reply[n] == '=') {
            snprintf(out, cap, "%.*s", (int)(len - n - 1), reply + n + 1);
            return;
        }
        reply += len;
        reply += strspn(reply, "\r\n");
    }
}
/* /proc/net/wireless stays zero on this driver; ask the supplicant instead. */
static void wifi_link(const char *home, int fd) {
    char status[4096], poll[512], state[40], ssid[200], rssi[16], speed[16], freq[16];
    char line[400], path[1200];
    if (control(fd, "STATUS", status, sizeof(status)))
        return;
    field(status, "wpa_state", state, sizeof(state));
    field(status, "ssid", ssid, sizeof(ssid));
    if (control(fd, "SIGNAL_POLL", poll, sizeof(poll)) || !strncmp(poll, "FAIL", 4))
        poll[0] = 0;
    field(poll, "RSSI", rssi, sizeof(rssi));
    field(poll, "LINKSPEED", speed, sizeof(speed));
    field(poll, "FREQUENCY", freq, sizeof(freq));
    snprintf(line, sizeof(line), "%s\t%s\t%s\t%s\t%s\n", state, rssi, speed, freq, ssid);
    snprintf(path, sizeof(path), "%s/wifi.link", home);
    atomic_write(path, line);
}
int wifi_scan(const char *home, const char *socket_path) {
    struct sockaddr_un local = {0}, remote = {0};
    local.sun_family = remote.sun_family = AF_UNIX;
    if (snprintf(local.sun_path, sizeof(local.sun_path), "%s.kf-%ld", socket_path,
                 (long)getpid()) >= (int)sizeof(local.sun_path) ||
        snprintf(remote.sun_path, sizeof(remote.sun_path), "%s", socket_path) >=
            (int)sizeof(remote.sun_path))
        return -1;
    int fd = socket(AF_UNIX, SOCK_DGRAM, 0);
    if (fd < 0)
        return -1;
    struct timeval timeout = {1, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    int result = -1;
    char reply[32768], cache[2048] = "", path[1200];
    if (bind(fd, (struct sockaddr *)&local, sizeof(local)))
        goto done;
    if (!geteuid() && chown(local.sun_path, 0, 1010))
        goto done;
    if (chmod(local.sun_path, 0660) || connect(fd, (struct sockaddr *)&remote, sizeof(remote)))
        goto done;
    wifi_link(home, fd);
    /* Results are from the previous asynchronous scan. Never block the UI. */
    if (control(fd, "SCAN_RESULTS", reply, sizeof(reply)))
        goto done;
    WifiNetwork networks[KF_NETWORKS];
    int count = wifi_parse(reply, networks, KF_NETWORKS);
    if (count < 0)
        goto done;
    size_t used = 0;
    for (int i = 0; i < count; i++)
        used += (size_t)snprintf(cache + used, sizeof(cache) - used, "%d\t%d\t%s\n",
                                 networks[i].signal, networks[i].security, networks[i].ssid);
    snprintf(path, sizeof(path), "%s/wifi.scan", home);
    result = atomic_write(path, cache);
    control(fd, "SCAN", reply, sizeof(reply));
done:
    close(fd);
    unlink(local.sun_path);
    return result;
}
int wifi_load(const char *home, WifiNetwork *networks, int capacity) {
    char path[1200], line[160];
    snprintf(path, sizeof(path), "%s/wifi.scan", home);
    FILE *f = fopen(path, "r");
    if (!f)
        return 0;
    int count = 0;
    while (count < capacity && fgets(line, sizeof(line), f)) {
        WifiNetwork entry = {0};
        char *first = strchr(line, '\t');
        char *ssid = first ? strchr(first + 1, '\t') : NULL;
        if (!ssid || sscanf(line, "%d\t%d", &entry.signal, &entry.security) != 2 ||
            entry.security < 0 || entry.security > 2)
            continue;
        ssid++;
        ssid[strcspn(ssid, "\r\n")] = 0;
        if (!*ssid || strlen(ssid) > 32)
            continue;
        strcpy(entry.ssid, ssid);
        networks[count++] = entry;
    }
    fclose(f);
    return count;
}
