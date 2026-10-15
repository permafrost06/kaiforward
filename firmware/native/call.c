#include "native.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

/* Local control is private to the existing radio owner. Never open a second
 * rild connection: doing so would displace the SMS service. Commands are
 * executed once; a disconnect or timeout must never replay a dial/answer. */
static int address(struct sockaddr_un *a, const char *home) {
    memset(a, 0, sizeof(*a));
    a->sun_family = AF_UNIX;
    return snprintf(a->sun_path, sizeof(a->sun_path), "%s/call.sock", home) >=
                   (int)sizeof(a->sun_path)
               ? -1
               : 0;
}
static void reply(int fd, const char *s) {
    send(fd, s, strlen(s), MSG_NOSIGNAL);
    close(fd);
}
void call_reply(Radio *r, int result) {
    if (r->control_fd < 0)
        return;
    char body[160];
    snprintf(body, sizeof(body), "{\"ok\":%s,\"result\":%d,\"uncertain\":%s}\n",
             result == 0 ? "true" : "false", result, result == -1 ? "true" : "false");
    reply(r->control_fd, body);
    r->control_fd = -1;
    r->control_token = 0;
}
int ussd_read(const Radio *r, char *json, size_t cap) {
    static const char *states[] = {"idle", "waiting", "reply", "done", "failed", "cancelled"};
    char message[KF_USSD * 6 + 8];
    if (json_string(message, sizeof(message), r->ussd_message) < 0)
        return -1;
    int n = snprintf(json, cap,
                     "{\"ok\":true,\"slot\":%d,\"id\":%d,\"state\":\"%s\",\"type\":%d,"
                     "\"error\":%d,\"message\":%s}\n",
                     r->slot, r->ussd_id, states[r->ussd_state], r->ussd_type, r->ussd_error,
                     message);
    return n < 0 || (size_t)n >= cap ? -1 : 0;
}
int call_listen(const char *home) {
    struct sockaddr_un a;
    if (address(&a, home))
        return -1;
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
    if (fd < 0)
        return -1;
    unlink(a.sun_path); /* Caller owns the exclusive SMS service lock. */
    if (bind(fd, (struct sockaddr *)&a, sizeof(a)) || chmod(a.sun_path, 0600) || listen(fd, 4)) {
        close(fd);
        return -1;
    }
    return fd;
}
void call_accept(int listener, Radio radios[2]) {
    int fd = accept4(listener, NULL, NULL, SOCK_CLOEXEC | SOCK_NONBLOCK);
    if (fd < 0)
        return;
    struct ucred peer;
    socklen_t size = sizeof(peer);
    if (getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &peer, &size) ||
        (peer.uid != 0 && peer.uid != geteuid())) {
        reply(fd, "{\"ok\":false,\"error\":\"Permission denied\"}\n");
        return;
    }
    /* A seqpacket client must send immediately. Poll bounds a broken client
     * without blocking radio notification processing for an unbounded read. */
    struct timeval timeout = {0, 100000};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    int flags = fcntl(fd, F_GETFL, 0);
    if (flags >= 0)
        fcntl(fd, F_SETFL, flags & ~O_NONBLOCK);
    char command[128], action[16], number[41] = "", extra;
    ssize_t n = recv(fd, command, sizeof(command) - 1, MSG_TRUNC);
    if (n <= 0 || n >= (ssize_t)sizeof(command) || memchr(command, 0, n)) {
        reply(fd, "{\"ok\":false,\"error\":\"Invalid command\"}\n");
        return;
    }
    command[n] = 0;
    int slot, fields = sscanf(command, "%d %15s %40s %c", &slot, action, number, &extra);
    if (fields < 2 || slot < 1 || slot > 2 || fields > 3) {
        reply(fd, "{\"ok\":false,\"error\":\"Invalid command\"}\n");
        return;
    }
    Radio *r = &radios[slot - 1];
    if (!strcmp(action, "status") && fields == 2) {
        char body[256];
        snprintf(body, sizeof(body),
                 "{\"ok\":true,\"slot\":%d,\"connected\":%s,"
                 "\"registered\":%s,\"calls\":%d,\"state\":%d,\"index\":%d}\n",
                 slot, r->fd >= 0 ? "true" : "false", r->registered == 1 ? "true" : "false",
                 r->calls, r->call_state, r->call_index);
        reply(fd, body);
        return;
    }
    if (!strcmp(action, "ussd-read") && fields == 2) {
        char body[KF_USSD * 6 + 160];
        reply(fd, ussd_read(r, body, sizeof(body)) ? "{\"ok\":false,\"error\":\"USSD unavailable\"}\n"
                                                   : body);
        return;
    }
    if (!strcmp(action, "ussd") || !strcmp(action, "ussd-cancel")) {
        /* Sent once like a call operation; the network's text arrives later. */
        int send = !action[4];
        if (fields != 2 + send || (send && radios[2 - slot].calls) ||
            radio_ussd(r, send ? number : NULL, fd))
            reply(fd, "{\"ok\":false,\"error\":\"USSD state does not allow this operation\"}\n");
        return;
    }
    int operation = !strcmp(action, "answer") && fields == 2   ? 1
                    : !strcmp(action, "hangup") && fields == 2 ? 2
                    : !strcmp(action, "dial") && fields == 3   ? 3
                                                               : 0;
    if (!operation || (operation != 2 && radios[2 - slot].calls) ||
        radio_call(r, operation, number, fd))
        reply(fd, "{\"ok\":false,\"error\":\"Call state does not allow this operation\"}\n");
}
int call_client(const char *home, int slot, const char *action, const char *number) {
    struct sockaddr_un a;
    if (slot < 1 || slot > 2 || address(&a, home))
        return 2;
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    struct timeval timeout = {8, 0};
    if (fd < 0)
        return 1;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    char command[128];
    int length = snprintf(command, sizeof(command), "%d %s%s%s", slot, action, number ? " " : "",
                          number ? number : "");
    if (length < 0 || length >= (int)sizeof(command) ||
        connect(fd, (struct sockaddr *)&a, sizeof(a)) ||
        send(fd, command, length, MSG_NOSIGNAL) != length) {
        close(fd);
        fprintf(stderr, "Native call controller unavailable\n");
        return 1;
    }
    char response[KF_USSD * 6 + 160];
    ssize_t n = recv(fd, response, sizeof(response) - 1, MSG_TRUNC);
    close(fd);
    if (n <= 0 || n >= (ssize_t)sizeof(response)) {
        fprintf(stderr, "Call result unknown; do not automatically retry\n");
        return 1;
    }
    response[n] = 0;
    fputs(response, stdout);
    return strstr(response, "\"ok\":true") ? 0 : 1;
}
