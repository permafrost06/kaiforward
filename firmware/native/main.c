#include "native.h"
#include <arpa/inet.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t running = 1;
static void stop(int sig) {
    (void)sig;
    running = 0;
}
static void quoted(FILE *f, const char *s) {
    fputc('"', f);
    for (; *s; s++) {
        if (*s == '"' || *s == '\\')
            fputc('\\', f);
        fputc(*s, f);
    }
    fputc('"', f);
}
int main(int argc, char **argv) {
    if (argc == 2 && !strcmp(argv[1], "--permission"))
        return permission_run();
    if (argc == 4 && !strcmp(argv[1], "--network-prefix")) {
        struct in_addr address, netmask;
        if (inet_pton(AF_INET, argv[2], &address) != 1 ||
            inet_pton(AF_INET, argv[3], &netmask) != 1)
            return 1;
        uint32_t mask = ntohl(netmask.s_addr), remaining = mask;
        int prefix = 0;
        while (remaining & 0x80000000u) {
            prefix++;
            remaining <<= 1;
        }
        if (remaining)
            return 1;
        address.s_addr = htonl(ntohl(address.s_addr) & mask);
        char ip[INET_ADDRSTRLEN];
        if (!inet_ntop(AF_INET, &address, ip, sizeof(ip)))
            return 1;
        printf("%s/%d\n", ip, prefix);
        return 0;
    }
    umask(0077);
    const char *home = KF_HOME, *sockets = "/dev/socket", *curl = "/system/bin/curl",
               *preview = NULL, *decode = NULL;
    int ui = 0, wifi = 0, upload = 0, scan = 0;
    int settings = 0, settings_port = 8768;
    const char *settings_bind = "0.0.0.0";
    int send_slot = 0, outbox = 0;
    int call_slot = 0;
    const char *call_action = NULL, *call_number = NULL;
    const char *send_recipient = NULL, *send_body = NULL;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--home") && i + 1 < argc)
            home = argv[++i];
        else if (!strcmp(argv[i], "--socket-dir") && i + 1 < argc)
            sockets = argv[++i];
        else if (!strcmp(argv[i], "--curl") && i + 1 < argc)
            curl = argv[++i];
        else if (!strcmp(argv[i], "--ui"))
            ui = 1;
        else if (!strcmp(argv[i], "--upload"))
            upload = 1;
        else if (!strcmp(argv[i], "--settings"))
            settings = 1;
        else if (!strcmp(argv[i], "--settings-set"))
            settings = 2;
        else if (!strcmp(argv[i], "--settings-key"))
            settings = 3;
        else if (!strcmp(argv[i], "--settings-bind") && i + 1 < argc)
            settings_bind = argv[++i];
        else if (!strcmp(argv[i], "--settings-port") && i + 1 < argc) {
            char *end;
            long port = strtol(argv[++i], &end, 10);
            if (!*argv[i] || *end || port < 1 || port > 65535) {
                fprintf(stderr, "Settings port must be between 1 and 65535\n");
                return 2;
            }
            settings_port = (int)port;
        } else if (!strcmp(argv[i], "--call") && i + 2 < argc) {
            const char *slot = argv[++i];
            call_slot = !strcmp(slot, "1") ? 1 : !strcmp(slot, "2") ? 2 : -1;
            call_action = argv[++i];
            if (!strcmp(call_action, "dial") && i + 1 < argc)
                call_number = argv[++i];
        } else if (!strcmp(argv[i], "--send") && i + 3 < argc) {
            const char *slot = argv[++i];
            send_slot = !strcmp(slot, "1") ? 1 : !strcmp(slot, "2") ? 2 : -1;
            send_recipient = argv[++i];
            send_body = argv[++i];
        } else if (!strcmp(argv[i], "--outbox"))
            outbox = 1;
        else if (!strcmp(argv[i], "--scan-wifi"))
            scan = 1;
        else if (!strcmp(argv[i], "--wifi-config"))
            wifi = 1;
        else if (!strcmp(argv[i], "--preview") && i + 1 < argc)
            preview = argv[++i];
        else if (!strcmp(argv[i], "--decode") && i + 1 < argc)
            decode = argv[++i];
        else {
            fprintf(stderr, "Usage: kaiforward-native [--home DIR] [--ui | --upload | "
                            "--wifi-config | --preview PPM | --decode PDU | --send SIM NUMBER TEXT "
                            "| --outbox | --settings | --settings-set | --settings-key] "
                            "[--settings-bind IPv4] [--settings-port PORT]\n");
            return 2;
        }
    }
    if (decode) {
        Sms sms;
        if (sms_decode(decode, &sms))
            return 1;
        char sender[800], body[KF_TEXT * 6];
        json_string(sender, sizeof(sender), sms.sender);
        json_string(body, sizeof(body), sms.body);
        printf("{\"sender\":%s,\"body\":%s,\"timestamp\":%lld,\"part\":%d,\"total\":%d}\n", sender,
               body, (long long)sms.timestamp, sms.part, sms.total);
        return 0;
    }
    if (call_action)
        return call_client(home, call_slot, call_action, call_number);
    if (scan)
        return wifi_scan(home, "/data/misc/wifi/sockets/wlan0") ? 1 : 0;
    if (wifi) {
        Config c;
        if (config_load(home, &c))
            memset(&c, 0, sizeof(c));
        puts("ctrl_interface=/data/misc/wifi/sockets\nupdate_config=0");
        if (!*c.ssid)
            return 0;
        puts("network={");
        printf("    ssid=");
        quoted(stdout, c.ssid);
        puts("");
        if (!*c.password)
            puts("    key_mgmt=NONE");
        else {
            printf("    psk=");
            if (strlen(c.password) == 64)
                printf("%s", c.password);
            else
                quoted(stdout, c.password);
            puts("");
        }
        puts("}");
        return 0;
    }
    if (ui || preview) {
        ui_run(home, preview);
        return 0;
    }
    if (mkdir(home, 0700) && access(home, W_OK)) {
        perror("Native storage");
        return 1;
    }
    if (settings)
        return settings_command(home, settings);
    if (upload) {
        uploader(home, curl);
        return 0;
    }
    sqlite3 *db = store_open(home);
    if (!db) {
        fprintf(stderr, "Native storage unavailable\n");
        return 1;
    }
    if (send_recipient) {
        int64_t job = store_send(db, send_slot, send_recipient, send_body);
        sqlite3_close(db);
        if (job < 0) {
            fprintf(stderr, "Invalid SMS: SIM 1/2, numeric recipient, maximum 70 UTF-16 units\n");
            return 1;
        }
        printf("Queued native SMS job %lld\n", (long long)job);
        return 0;
    }
    if (outbox) {
        sqlite3_stmt *q = NULL;
        if (sqlite3_prepare_v2(db, "SELECT id,slot,status FROM outbox ORDER BY id DESC LIMIT 50",
                               -1, &q, NULL) != SQLITE_OK) {
            sqlite3_close(db);
            return 1;
        }
        while (sqlite3_step(q) == SQLITE_ROW)
            printf("Job %lld SIM %d %s\n", (long long)sqlite3_column_int64(q, 0),
                   sqlite3_column_int(q, 1), sqlite3_column_text(q, 2));
        sqlite3_finalize(q);
        sqlite3_close(db);
        return 0;
    }
    int lock = service_lock(home, "sms");
    if (lock < 0) {
        fprintf(stderr, "Native SMS service is already running / lock unavailable\n");
        sqlite3_close(db);
        return 1;
    }
    store_uncertain(db, 0);
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    signal(SIGPIPE, SIG_IGN);
    radio_trace_home(home);
    Radio radios[2];
    radio_init(&radios[0], 1);
    radio_init(&radios[1], 2);
    int controller = call_listen(home);
    if (controller < 0)
        fprintf(stderr, "Native call control unavailable; SMS remains active\n");
    if (settings_http_open(home, settings_bind, settings_port) < 0)
        fprintf(stderr, "Native settings HTTP unavailable; SMS remains active\n");
    char path[1200], status[500];
    snprintf(path, sizeof(path), "%s/radio.status", home);
    int64_t next = 0;
    int previous_queued = 0;
    while (running) {
        for (int i = 0; i < 2; i++) {
            radio_tick(&radios[i], db, sockets);
            if (radios[i].fd < 0)
                store_uncertain(db, i + 1);
            radio_send_queued(&radios[i], db);
        }
        struct pollfd fds[12];
        for (int i = 0; i < 2; i++) {
            fds[i].fd = radios[i].fd;
            fds[i].events = POLLIN;
        }
        fds[2].fd = controller;
        fds[2].events = POLLIN;
        int nfds = 3 + settings_http_fds(fds + 3);
        if (poll(fds, (nfds_t)nfds, 1000) > 0) {
            for (int i = 0; i < 2; i++)
                if (fds[i].revents & (POLLIN | POLLHUP | POLLERR))
                    radio_read(&radios[i], db);
            if (fds[2].revents & POLLIN)
                call_accept(controller, radios);
        }
        settings_http_tick(home, db, radios);
        int current_queued = store_count(db, "messages", "uploaded=0");
        if (current_queued > previous_queued) {
            char syncpath[1200];
            snprintf(syncpath, sizeof(syncpath), "%s/sync", home);
            atomic_write(syncpath, "");
        }
        previous_queued = current_queued;
        if (monotonic_ms() >= next) {
            int queued = store_count(db, "messages", "uploaded=0"),
                blocked = store_count(db, "raw", "decoded=0 AND type!=1004"),
                parts = store_count(db, "groups", "complete=0");
            blocked += store_count(db, "outbox", "status IN ('uncertain','failed')");
            parts += store_count(db, "outbox", "status IN ('queued','submitting')");
            int received = store_count(db, "messages", "direction='received'");
            char call[80] = "Call: idle";
            for (int i = 0; i < 2; i++)
                if (radios[i].calls)
                    snprintf(call, sizeof(call), "Call: SIM %d %s", i + 1,
                             radios[i].incoming ? "incoming" : "active");
            snprintf(status, sizeof(status),
                     "SIM 1: %s\nSIM 2: %s\nQueued: %d  Received: %d\nReview: %d / Waiting: "
                     "%d\n%lld\n%s\n",
                     radio_label(&radios[0]), radio_label(&radios[1]), queued, received, blocked, parts,
                     (long long)now_ms(), call);
            atomic_write(path, status);
            next = monotonic_ms() + 5000;
        }
    }
    for (int i = 0; i < 2; i++)
        radio_close(&radios[i]);
    sqlite3_close(db);
    settings_http_close();
    if (controller >= 0)
        close(controller);
    close(lock);
    return 0;
}
