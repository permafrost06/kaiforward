#include "native.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#define POWER "/sys/class/power_supply/"

static int read_line(const char *path, char *out, size_t cap) {
    FILE *f = fopen(path, "r");
    if (!f)
        return -1;
    int ok = fgets(out, (int)cap, f) != NULL;
    fclose(f);
    if (!ok)
        return -1;
    out[strcspn(out, "\r\n")] = 0;
    return 0;
}
static int read_int(const char *path, long *value) {
    char line[64], *end;
    if (read_line(path, line, sizeof(line)) || !*line)
        return -1;
    *value = strtol(line, &end, 10);
    return *end ? -1 : 0;
}
int battery_level(void) {
    long n;
    return read_int(POWER "battery/capacity", &n) || n < 0 || n > 100 ? -1 : (int)n;
}
int wifi_connected(void) {
    long n;
    return !read_int("/sys/class/net/wlan0/carrier", &n) && n == 1;
}
const char *radio_label(const Radio *r) {
    if (r->fd < 0)
        return "disconnected";
    if (r->sim == 0)
        return "absent";
    if (r->sim == 2)
        return "SIM error";
    if (r->active_app == 2 || r->active_app == 3)
        return "PIN/PUK locked";
    if (r->sim != 1 || r->active_app != 5)
        return "checking SIM";
    if (r->registered == 1 && r->radio == 10)
        return "registered";
    return "searching";
}
static const char *call_label(const Radio *r) {
    if (!r->calls)
        return "idle";
    /* RIL_CallState; only the first call is tracked. */
    static const char *states[] = {"active", "holding", "dialing", "alerting", "incoming",
                                   "waiting"};
    return r->call_state >= 0 && r->call_state <= 5 ? states[r->call_state] : "unknown";
}
static const char *sim_card(const Radio *r) {
    if (r->fd < 0 || r->sim < 0)
        return "unknown";
    if (r->sim == 0)
        return "absent";
    if (r->sim == 2)
        return "error";
    return r->active_app == 5   ? "ready"
           : r->active_app == 2 ? "pin"
           : r->active_app == 3 ? "puk"
                                : "initializing";
}
static int wifi_address(char *out, size_t cap) {
    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    struct ifreq ifr;
    memset(&ifr, 0, sizeof(ifr));
    snprintf(ifr.ifr_name, sizeof(ifr.ifr_name), "wlan0");
    int rc = ioctl(fd, SIOCGIFADDR, &ifr);
    close(fd);
    if (rc || ifr.ifr_addr.sa_family != AF_INET)
        return -1;
    struct in_addr a = ((struct sockaddr_in *)&ifr.ifr_addr)->sin_addr;
    return a.s_addr && inet_ntop(AF_INET, &a, out, (socklen_t)cap) ? 0 : -1;
}
/* wifi.link: "<wpa_state>\t<rssi>\t<Mbps>\t<MHz>\t<ssid>", refreshed by --scan-wifi. */
typedef struct {
    char state[40], ssid[200];
    long rssi, speed, freq;
} Link;
static int wifi_link(const char *home, Link *l) {
    char path[1200], line[400], *fields[5], *end;
    snprintf(path, sizeof(path), "%s/wifi.link", home);
    struct stat st;
    /* The Wi-Fi service polls every ~10 s; ignore leftovers if it stopped. */
    if (stat(path, &st) || time(NULL) - st.st_mtime > 60 || read_line(path, line, sizeof(line)))
        return -1;
    fields[0] = line;
    for (int i = 1; i < 5; i++) {
        char *tab = strchr(fields[i - 1], '\t');
        if (!tab)
            return -1;
        *tab = 0;
        fields[i] = tab + 1;
    }
    snprintf(l->state, sizeof(l->state), "%s", fields[0]);
    snprintf(l->ssid, sizeof(l->ssid), "%s", fields[4]);
    long *numbers[] = {&l->rssi, &l->speed, &l->freq};
    for (int i = 0; i < 3; i++) {
        *numbers[i] = strtol(fields[i + 1], &end, 10);
        if (!*fields[i + 1] || *end)
            *numbers[i] = 0;
    }
    return 0;
}
static void lower(char *s) {
    for (; *s; s++)
        *s = (char)tolower((unsigned char)*s);
}

int status_read(const char *home, sqlite3 *db, const Radio radios[2], char *json, size_t cap) {
    size_t n = 0;
#define ADD(...)                                                                                   \
    do {                                                                                           \
        int added = snprintf(json + n, cap - n, __VA_ARGS__);                                      \
        if (added < 0 || (size_t)added >= cap - n)                                                 \
            return -1;                                                                             \
        n += (size_t)added;                                                                        \
    } while (0)
    long value;
    double uptime = 0;
    FILE *f = fopen("/proc/uptime", "r");
    if (f) {
        if (fscanf(f, "%lf", &uptime) != 1)
            uptime = 0;
        fclose(f);
    }
    ADD("{\"time\":%lld,\"uptime\":%lld,", (long long)now_ms(), (long long)uptime);

    char text[64], quoted[160];
    int level = battery_level();
    if (level < 0)
        ADD("\"battery\":{\"level\":null");
    else
        ADD("\"battery\":{\"level\":%d", level);
    int charging = 0;
    if (read_line(POWER "battery/status", text, sizeof(text)) || !*text) {
        ADD(",\"status\":null");
    } else {
        lower(text);
        charging = !strcmp(text, "charging");
        json_string(quoted, sizeof(quoted), text);
        ADD(",\"status\":%s", quoted);
    }
    long usb = 0, ac = 0;
    if (read_int(POWER "usb/online", &usb))
        usb = 0;
    if (read_int(POWER "ac/online", &ac))
        ac = 0;
    ADD(",\"charging\":%s,\"plugged\":%s", charging ? "true" : "false",
        usb == 1 ? "\"usb\"" : ac == 1 ? "\"ac\"" : "null");
    /* Kernel units: tenths of a degree Celsius and microvolts. */
    if (read_int(POWER "battery/temp", &value))
        ADD(",\"temperature\":null");
    else
        ADD(",\"temperature\":%.1f", (double)value / 10);
    if (read_int(POWER "battery/voltage_now", &value))
        ADD(",\"voltage_mv\":null},");
    else
        ADD(",\"voltage_mv\":%ld},", value / 1000);

    Config c;
    if (config_load(home, &c))
        memset(&c, 0, sizeof(c));
    char ssid[200], address[INET_ADDRSTRLEN];
    if (json_string(ssid, sizeof(ssid), c.ssid) < 0)
        return -1;
    int connected = wifi_connected();
    ADD("\"wifi\":{\"connected\":%s,\"configured_ssid\":%s", connected ? "true" : "false",
        ssid);
    Link link;
    if (wifi_link(home, &link)) {
        ADD(",\"state\":null,\"ssid\":null");
        memset(&link, 0, sizeof(link));
    } else {
        lower(link.state);
        json_string(quoted, sizeof(quoted), link.state);
        if (json_string(ssid, sizeof(ssid), link.ssid) < 0)
            return -1;
        ADD(",\"state\":%s,\"ssid\":%s", quoted, *link.ssid ? ssid : "null");
    }
    if (connected && !wifi_address(address, sizeof(address)))
        ADD(",\"ip\":\"%s\"", address);
    else
        ADD(",\"ip\":null");
    if (connected && link.rssi < 0 && link.rssi > -200)
        ADD(",\"signal_dbm\":%ld", link.rssi);
    else
        ADD(",\"signal_dbm\":null");
    if (connected && link.speed > 0)
        ADD(",\"link_mbps\":%ld", link.speed);
    else
        ADD(",\"link_mbps\":null");
    if (connected && link.freq > 0)
        ADD(",\"frequency_mhz\":%ld},", link.freq);
    else
        ADD(",\"frequency_mhz\":null},");

    ADD("\"sims\":[");
    int call_slot = 0;
    for (int i = 0; i < 2; i++) {
        const Radio *r = &radios[i];
        if (r->calls && !call_slot)
            call_slot = i + 1;
        ADD("%s{\"slot\":%d,\"state\":\"%s\",\"connected\":%s,\"radio_on\":%s,\"sim\":\"%s\","
            "\"registered\":%s,\"call\":\"%s\"}",
            i ? "," : "", r->slot, radio_label(r), r->fd >= 0 ? "true" : "false",
            r->fd >= 0 && r->radio == 10 ? "true" : "false", sim_card(r),
            r->fd >= 0 && r->registered == 1 ? "true" : "false", call_label(r));
    }
    ADD("],");

    if (call_slot)
        ADD("\"call\":{\"active\":true,\"slot\":%d,\"state\":\"%s\",\"incoming\":%s},", call_slot,
            call_label(&radios[call_slot - 1]),
            radios[call_slot - 1].incoming ? "true" : "false");
    else
        ADD("\"call\":{\"active\":false,\"slot\":null,\"state\":\"idle\",\"incoming\":false},");

    ADD("\"messages\":{\"pending_upload\":%d,\"received\":%d,\"sent\":%d,"
        "\"undecoded\":%d,\"incomplete\":%d,",
        store_count(db, "messages", "uploaded=0"),
        store_count(db, "messages", "direction='received'"),
        store_count(db, "messages", "direction='sent'"),
        store_count(db, "raw", "decoded=0 AND type!=1004"),
        store_count(db, "groups", "complete=0"));
    ADD("\"outbox\":{\"queued\":%d,\"submitting\":%d,\"sent\":%d,\"failed\":%d,"
        "\"uncertain\":%d}},",
        store_count(db, "outbox", "status='queued'"),
        store_count(db, "outbox", "status='submitting'"),
        store_count(db, "outbox", "status='sent'"), store_count(db, "outbox", "status='failed'"),
        store_count(db, "outbox", "status='uncertain'"));

    /* upload.status: "<ms>\n<STATE>\n", written by the uploader process. */
    char path[1200], stamp[32] = "", state[64] = "";
    snprintf(path, sizeof(path), "%s/upload.status", home);
    f = fopen(path, "r");
    if (f) {
        if (!fgets(stamp, sizeof(stamp), f) || !fgets(state, sizeof(state), f))
            stamp[0] = state[0] = 0;
        fclose(f);
    }
    stamp[strcspn(stamp, "\r\n")] = 0;
    state[strcspn(state, "\r\n")] = 0;
    const char *upload = !strcmp(state, "SYNCING")       ? "syncing"
                         : !strcmp(state, "QUEUE EMPTY") ? "idle"
                         : !strncmp(state, "RETRYING", 8) ? "retrying"
                                                          : NULL;
    char *end;
    long long at = strtoll(stamp, &end, 10);
    ADD("\"upload\":{\"configured\":%s,\"state\":", *c.url && *c.token ? "true" : "false");
    if (upload)
        ADD("\"%s\"", upload);
    else
        ADD("null");
    if (*stamp && !*end && at > 0)
        ADD(",\"updated\":%lld}}\n", at);
    else
        ADD(",\"updated\":null}}\n");
#undef ADD
    return 0;
}
