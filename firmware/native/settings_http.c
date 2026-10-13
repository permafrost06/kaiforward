#include "native.h"
#include <arpa/inet.h>
#include <ctype.h>
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#define CLIENTS 8
#define HEADER_LIMIT 4096
#define BODY_LIMIT 16384
#define RESPONSE_LIMIT 8192

typedef struct {
    int fd, writing;
    int64_t deadline;
    size_t used, length, sent;
    char input[HEADER_LIMIT + BODY_LIMIT + 1];
    char output[RESPONSE_LIMIT];
} Client;

static int listener = -1;
static char access_key[65];
static Client clients[CLIENTS];

static void close_client(Client *c) {
    close(c->fd);
    memset(c, 0, sizeof(*c));
    c->fd = -1;
}

void settings_http_close(void) {
    if (listener >= 0)
        close(listener);
    listener = -1;
    for (int i = 0; i < CLIENTS; i++)
        if (clients[i].fd >= 0)
            close_client(&clients[i]);
}

int settings_http_open(const char *home, const char *address, int port) {
    for (int i = 0; i < CLIENTS; i++)
        clients[i].fd = -1;
    struct sockaddr_in addr = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    if (port < 1 || port > 65535 || inet_pton(AF_INET, address, &addr.sin_addr) != 1 ||
        settings_key(home, access_key))
        return -1;
    listener = socket(AF_INET, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (listener < 0)
        return -1;
    int one = 1;
    setsockopt(listener, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    if (bind(listener, (struct sockaddr *)&addr, sizeof(addr)) || listen(listener, CLIENTS)) {
        close(listener);
        listener = -1;
        return -1;
    }
    return listener;
}

int settings_http_fds(struct pollfd *fds) {
    int n = 0;
    if (listener >= 0)
        fds[n++] = (struct pollfd){.fd = listener, .events = POLLIN};
    for (int i = 0; i < CLIENTS; i++)
        if (clients[i].fd >= 0)
            fds[n++] = (struct pollfd){.fd = clients[i].fd,
                                       .events = clients[i].writing ? POLLOUT : POLLIN};
    return n;
}

static void respond(Client *c, int status, const char *body) {
    const char *reason = status == 200   ? "OK"
                         : status == 400 ? "Bad Request"
                         : status == 401 ? "Unauthorized"
                         : status == 404 ? "Not Found"
                         : status == 405 ? "Method Not Allowed"
                         : status == 409 ? "Conflict"
                         : status == 413 ? "Content Too Large"
                         : status == 415 ? "Unsupported Media Type"
                                         : "Internal Server Error";
    const char *extra = status == 401   ? "WWW-Authenticate: Bearer\r\n"
                        : status == 405 ? "Allow: GET, PATCH\r\n"
                                        : "";
    if (!body)
        body = status == 400   ? "{\"error\":\"invalid settings request\"}\n"
               : status == 401 ? "{\"error\":\"settings access key required\"}\n"
               : status == 409 ? "{\"error\":\"settings busy; retry\"}\n"
               : status == 500 ? "{\"error\":\"settings storage unavailable\"}\n"
                               : "{\"error\":\"unsupported request\"}\n";
    int n = snprintf(c->output, sizeof(c->output),
                     "HTTP/1.1 %d %s\r\nContent-Type: "
                     "application/json\r\nContent-Length: %zu\r\n"
                     "Connection: close\r\nCache-Control: no-store\r\n%s\r\n%s",
                     status, reason, strlen(body), extra, body);
    if (n < 0 || (size_t)n >= sizeof(c->output)) {
        close_client(c);
        return;
    }
    c->length = (size_t)n;
    c->writing = 1;
    c->deadline = monotonic_ms() + 5000;
}

static char *trim(char *s) {
    while (*s == ' ' || *s == '\t')
        s++;
    size_t n = strlen(s);
    while (n && (s[n - 1] == ' ' || s[n - 1] == '\t'))
        s[--n] = 0;
    return s;
}

static int authorized(const char *value) {
    if (strlen(value) != 71 || strncmp(value, "Bearer ", 7))
        return 0;
    unsigned diff = 0;
    for (int i = 0; i < 64; i++)
        diff |= (unsigned char)value[i + 7] ^ (unsigned char)access_key[i];
    return diff == 0;
}

static void request(Client *c, const char *home, sqlite3 *db) {
    char *end = strstr(c->input, "\r\n\r\n");
    if (!end) {
        if (c->used >= HEADER_LIMIT)
            respond(c, 413, NULL);
        return;
    }
    size_t header_size = (size_t)(end - c->input) + 4;
    if (header_size > HEADER_LIMIT) {
        respond(c, 413, NULL);
        return;
    }
    /* Parse a copy: the body can arrive in a later nonblocking read. */
    char header[HEADER_LIMIT + 1];
    memcpy(header, c->input, header_size);
    header[header_size - 2] = 0;
    char *line_end = strstr(header, "\r\n");
    if (!line_end) {
        respond(c, 400, NULL);
        return;
    }
    *line_end = 0;
    char method[16], path[128], version[16], extra;
    if (sscanf(header, "%15s %127s %15s %c", method, path, version, &extra) != 3 ||
        (strcmp(version, "HTTP/1.1") && strcmp(version, "HTTP/1.0"))) {
        respond(c, 400, NULL);
        return;
    }
    size_t body_size = 0;
    int has_length = 0, auth = 0, has_auth = 0, json = 0, has_type = 0;
    char *line = line_end + 2;
    while (*line) {
        line_end = strstr(line, "\r\n");
        if (!line_end) {
            respond(c, 400, NULL);
            return;
        }
        *line_end = 0;
        char *colon = strchr(line, ':');
        if (!colon || colon == line || *line == ' ' || *line == '\t') {
            respond(c, 400, NULL);
            return;
        }
        *colon++ = 0;
        char *value = trim(colon);
        if (!strcasecmp(line, "Authorization")) {
            if (has_auth++) {
                respond(c, 400, NULL);
                return;
            }
            auth = authorized(value);
        } else if (!strcasecmp(line, "Content-Length")) {
            if (has_length++ || !*value) {
                respond(c, 400, NULL);
                return;
            }
            for (char *p = value; *p; p++) {
                if (!isdigit((unsigned char)*p)) {
                    respond(c, 400, NULL);
                    return;
                }
                body_size = body_size * 10 + (unsigned)(*p - '0');
                if (body_size > BODY_LIMIT) {
                    respond(c, 413, NULL);
                    return;
                }
            }
        } else if (!strcasecmp(line, "Content-Type")) {
            if (has_type++) {
                respond(c, 400, NULL);
                return;
            }
            char *semicolon = strchr(value, ';');
            if (semicolon)
                *semicolon = 0;
            json = !strcasecmp(trim(value), "application/json");
        } else if (!strcasecmp(line, "Transfer-Encoding") || !strcasecmp(line, "Expect") ||
                   !strcasecmp(line, "Origin")) {
            respond(c, 400, NULL);
            return;
        }
        line = line_end + 2;
    }
    if (!auth) {
        respond(c, 401, NULL);
        return;
    }
    if (strcmp(path, "/v1/settings")) {
        respond(c, 404, NULL);
        return;
    }
    int patch = !strcmp(method, "PATCH");
    if (!patch && strcmp(method, "GET")) {
        respond(c, 405, NULL);
        return;
    }
    if (patch && !json) {
        respond(c, 415, NULL);
        return;
    }
    if ((patch && (!has_length || !body_size)) || (!patch && body_size)) {
        respond(c, 400, NULL);
        return;
    }
    if (c->used < header_size + body_size)
        return;
    if (c->used != header_size + body_size) {
        respond(c, 400, NULL);
        return;
    }
    int result = patch ? settings_update(db, home, c->input + header_size) : 0;
    char body[7600];
    if (result)
        respond(c, result, NULL);
    else if (settings_read(home, body, sizeof(body)))
        respond(c, 500, NULL);
    else
        respond(c, 200, body);
}

void settings_http_tick(const char *home, sqlite3 *db) {
    if (listener < 0)
        return;
    for (int i = 0; i < CLIENTS; i++) {
        Client *c = &clients[i];
        if (c->fd < 0)
            continue;
        if (monotonic_ms() >= c->deadline) {
            close_client(c);
            continue;
        }
        if (!c->writing) {
            ssize_t n = recv(c->fd, c->input + c->used, sizeof(c->input) - 1 - c->used, 0);
            if (n > 0) {
                if (memchr(c->input + c->used, 0, (size_t)n)) {
                    respond(c, 400, NULL);
                } else {
                    c->used += (size_t)n;
                    c->input[c->used] = 0;
                    request(c, home, db);
                }
            } else if (!n || (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)) {
                close_client(c);
            }
        }
        if (c->fd >= 0 && c->writing) {
            ssize_t n = send(c->fd, c->output + c->sent, c->length - c->sent, MSG_NOSIGNAL);
            if (n > 0) {
                c->sent += (size_t)n;
                if (c->sent == c->length)
                    close_client(c);
            } else if (n < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) {
                close_client(c);
            }
        }
    }
    /* Bound accepts per tick so remote requests cannot starve modem work. */
    for (int i = 0; i < CLIENTS; i++) {
        int fd = accept4(listener, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (fd < 0)
            break;
        int slot = 0;
        while (slot < CLIENTS && clients[slot].fd >= 0)
            slot++;
        if (slot == CLIENTS) {
            close(fd);
            continue;
        }
        clients[slot].fd = fd;
        clients[slot].deadline = monotonic_ms() + 5000;
    }
}
