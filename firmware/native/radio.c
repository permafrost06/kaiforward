#include "native.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

static char trace_path[1200];
void radio_trace_home(const char *home) {
    snprintf(trace_path, sizeof(trace_path), "%s/radio.events", home);
}
static void trace(Radio *r, const char *event, int code, int error) {
    if (!*trace_path)
        return;
    struct stat st;
    if (!stat(trace_path, &st) && st.st_size > 65536) {
        char previous[1210];
        snprintf(previous, sizeof(previous), "%s.previous", trace_path);
        rename(trace_path, previous);
    }
    FILE *f = fopen(trace_path, "a");
    if (!f)
        return;
    fprintf(f, "%lld SIM%d %s code=%d error=%d\n", (long long)now_ms(), r->slot, event, code,
            error);
    fclose(f);
}

int parcel_int(Parcel *p) {
    if (p->offset + 4 > p->size) {
        p->error = 1;
        return 0;
    }
    const unsigned char *v = p->data + p->offset;
    p->offset += 4;
    return (int)((uint32_t)v[0] | (uint32_t)v[1] << 8 | (uint32_t)v[2] << 16 |
                 (uint32_t)v[3] << 24);
}
int parcel_string(Parcel *p, char *out, size_t cap) {
    int n = parcel_int(p);
    if (p->error)
        return -1;
    if (n == -1) {
        if (cap)
            out[0] = 0;
        return 0;
    }
    if (n < 0 || (size_t)n >= cap || p->offset + ((size_t)n + 1) * 2 > p->size) {
        p->error = 1;
        return -1;
    }
    size_t used = 0;
    for (int i = 0; i < n; i++) {
        unsigned cp = p->data[p->offset] | (unsigned)p->data[p->offset + 1] << 8;
        p->offset += 2;
        if (cp >= 0xd800 && cp <= 0xdbff && i + 1 < n) {
            unsigned low = p->data[p->offset] | (unsigned)p->data[p->offset + 1] << 8;
            if (low < 0xdc00 || low > 0xdfff) {
                p->error = 1;
                return -1;
            }
            cp = 0x10000 + (cp - 0xd800) * 1024 + low - 0xdc00;
            p->offset += 2;
            i++;
        }
        size_t width = cp < 128 ? 1 : cp < 2048 ? 2 : cp < 65536 ? 3 : 4;
        if (!cp || (cp >= 0xd800 && cp <= 0xdfff) || used + width >= cap) {
            p->error = 1;
            return -1;
        }
        if (width == 1)
            out[used++] = (char)cp;
        else {
            out[used++] = (char)((width == 2   ? 0xc0
                                  : width == 3 ? 0xe0
                                               : 0xf0) |
                                 (cp >> ((width - 1) * 6)));
            for (size_t k = width - 1; k > 0; k--)
                out[used++] = (char)(0x80 | ((cp >> ((k - 1) * 6)) & 63));
        }
    }
    if (p->data[p->offset] || p->data[p->offset + 1]) {
        p->error = 1;
        return -1;
    }
    p->offset = (p->offset + 2 + 3) & ~(size_t)3;
    if (p->offset > p->size) {
        p->error = 1;
        return -1;
    }
    out[used] = 0;
    return 0;
}
static void put(unsigned char *p, int v) {
    uint32_t n = (uint32_t)v;
    for (int i = 0; i < 4; i++)
        p[i] = (unsigned char)(n >> (i * 8));
}
static size_t put_string(unsigned char *p, const char *s) {
    if (!s) {
        put(p, -1);
        return 4;
    }
    size_t n = strlen(s);
    put(p, (int)n);
    memset(p + 4, 0, ((n + 1) * 2 + 3) & ~(size_t)3);
    for (size_t i = 0; i < n; i++)
        p[4 + i * 2] = (unsigned char)s[i];
    return 4 + (((n + 1) * 2 + 3) & ~(size_t)3);
}
static void ussd_set(Radio *r, int state, int type, int error, const char *message) {
    size_t n = strlen(message);
    if (n >= sizeof(r->ussd_message)) {
        /* Never split a UTF-8 sequence; real USSD text is far shorter. */
        n = sizeof(r->ussd_message) - 1;
        while (n && ((unsigned char)message[n] & 0xc0) == 0x80)
            n--;
    }
    memcpy(r->ussd_message, message, n);
    r->ussd_message[n] = 0;
    r->ussd_state = state;
    r->ussd_type = type;
    r->ussd_error = error;
    r->ussd_token = 0;
    r->ussd_id++;
}
void radio_close(Radio *r) {
    call_reply(r, -1);
    if (r->ussd_state == USSD_WAITING || r->ussd_state == USSD_REPLY)
        ussd_set(r, USSD_FAILED, -1, -1, "");
    if (r->fd >= 0) {
        trace(r, "disconnect", 0, 0);
        close(r->fd);
    }
    r->fd = -1;
    r->used = 0;
    r->radio = -1;
    r->sim = -1;
    r->registered = -1;
    r->active_app = -1;
    r->calls = r->incoming = 0;
    r->call_state = -2;
    r->call_index = 0;
    r->subscription_attempted = 0;
    r->app_type = 0;
    r->aid[0] = 0;
    memset(r->pending, 0, sizeof(r->pending));
    memset(r->pending_job, 0, sizeof(r->pending_job));
    r->retry_at = monotonic_ms() + 5000;
}
void radio_init(Radio *r, int slot) {
    memset(r, 0, sizeof(*r));
    r->fd = -1;
    r->control_fd = -1;
    r->call_state = -2;
    r->slot = slot;
    r->radio = r->sim = r->registered = r->active_app = -1;
    r->ussd_type = -1;
}
static int request(Radio *r, int code, const unsigned char *args, size_t size) {
    if (r->fd < 0 || size > 1000)
        return -1;
    int token = ++r->serial;
    if (r->serial >= 1000000000)
        r->serial = 0;
    int index = token % 256;
    if (r->pending[index]) {
        radio_close(r);
        return -1;
    }
    unsigned char frame[1024];
    size_t length = size + 8;
    frame[0] = (unsigned char)(length >> 24);
    frame[1] = (unsigned char)(length >> 16);
    frame[2] = (unsigned char)(length >> 8);
    frame[3] = (unsigned char)length;
    put(frame + 4, code);
    put(frame + 8, token);
    if (size)
        memcpy(frame + 12, args, size);
    size_t sent = 0;
    while (sent < size + 12) {
        ssize_t n = send(r->fd, frame + sent, size + 12 - sent, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR)
            continue;
        if (n <= 0) {
            radio_close(r);
            return -1;
        }
        sent += (size_t)n;
    }
    r->pending[index] = code;
    r->pending_at[index] = monotonic_ms();
    if (code == 20)
        r->registration_retry_at = r->pending_at[index] + 15000;
    trace(r, "request", code, 0);
    return token;
}
static void ints(Radio *r, int code, int count, int a, int b) {
    unsigned char args[12];
    put(args, count);
    put(args + 4, a);
    put(args + 8, b);
    request(r, code, args, (size_t)(count + 1) * 4);
}
int radio_call(Radio *r, int action, const char *number, int client) {
    if (r->fd < 0 || r->control_fd >= 0 || r->call_state < -1)
        return -1;
    unsigned char args[128];
    size_t size = 0;
    int code;
    if (action == 1 && r->calls == 1 && r->call_state == 4)
        code = 40; /* RIL_REQUEST_ANSWER */
    else if (action == 2 && r->calls == 1 && r->call_index > 0) {
        code = 12; /* RIL_REQUEST_HANGUP, one explicit call index */
        put(args, 1);
        put(args + 4, r->call_index);
        size = 8;
    } else if (action == 3 && r->calls == 0 && r->registered == 1 && r->active_app == 5) {
        size_t n = strlen(number);
        if (!n || n > 40)
            return -1;
        for (size_t i = 0; i < n; i++)
            if ((number[i] < '0' || number[i] > '9') && !(i == 0 && number[i] == '+' && n > 1))
                return -1;
        code = 10; /* RIL_REQUEST_DIAL */
        size = put_string(args, number);
        put(args + size, 0);     /* Default CLIR. */
        put(args + size + 4, 0); /* No UUS. */
        size += 8;
    } else
        return -1;
    int token = request(r, code, args, size);
    if (token < 1)
        return -1;
    r->control_fd = client;
    r->control_token = token;
    r->control_at = monotonic_ms();
    return 0;
}
int radio_ussd(Radio *r, const char *code, int client) {
    if (r->fd < 0 || r->control_fd >= 0 || r->sim != 1 || r->active_app != 5 ||
        r->registered != 1)
        return -1;
    unsigned char args[128];
    size_t size = 0;
    int request_code = 30; /* RIL_REQUEST_CANCEL_USSD */
    if (code) {
        /* A new code, or the answer while the network waits for one. */
        size_t n = strlen(code);
        if (!n || n > 40 || r->ussd_state == USSD_WAITING)
            return -1;
        for (size_t i = 0; i < n; i++)
            if ((code[i] < '0' || code[i] > '9') && code[i] != '*' && code[i] != '#' &&
                code[i] != '+')
                return -1;
        request_code = 29; /* RIL_REQUEST_SEND_USSD */
        size = put_string(args, code);
    } else if (r->ussd_state != USSD_WAITING && r->ussd_state != USSD_REPLY)
        return -1;
    int token = request(r, request_code, args, size);
    if (token < 1)
        return -1;
    r->control_fd = client;
    r->control_token = token;
    r->control_at = monotonic_ms();
    if (code) {
        ussd_set(r, USSD_WAITING, -1, 0, "");
        r->ussd_token = token;
        r->ussd_since = monotonic_ms();
    }
    return 0;
}
static void ack(Radio *r, int success) { ints(r, 37, 2, success, success ? 0 : 0xd3); }
static void sim_record(Radio *r, int index) {
    if (index < 1 || index > 255)
        return;
    unsigned char a[512];
    size_t n = 0;
    put(a + n, 0xb2);
    n += 4;
    put(a + n, 0x6f3c);
    n += 4;
    n += put_string(a + n, r->app_type == 2 ? "3F007FFF" : "3F007F10");
    put(a + n, index);
    n += 4;
    put(a + n, 4);
    n += 4;
    put(a + n, 176);
    n += 4;
    n += put_string(a + n, NULL);
    n += put_string(a + n, NULL);
    n += put_string(a + n, *r->aid ? r->aid : NULL);
    request(r, 28, a, n);
}
void radio_send_queued(Radio *r, sqlite3 *db) {
    if (r->fd < 0 || r->radio != 10 || r->sim != 1 || r->active_app != 5)
        return;
    for (int i = 0; i < 256; i++)
        if (r->pending_job[i] || r->pending[i] == 20)
            return;
    int64_t now = monotonic_ms();
    if (r->registered != 1 || now >= r->registration_retry_at) {
        /* Modems can miss registration notifications while idle. Query again
         * only when a job is waiting, and never submit against stale state. */
        if (now < r->registration_retry_at)
            return;
        char where[64];
        snprintf(where, sizeof(where), "slot=%d AND status='queued'", r->slot);
        if (store_count(db, "outbox", where) > 0)
            request(r, 20, NULL, 0);
        return;
    }
    char pdu[400];
    int64_t job = store_outgoing(db, r->slot, pdu, sizeof(pdu));
    if (job <= 0)
        return;
    unsigned char args[900];
    size_t size = 0;
    put(args, 2);
    size += 4;
    size += put_string(args + size, NULL);
    size += put_string(args + size, pdu);
    int token = request(r, 25, args, size);
    if (token > 0)
        r->pending_job[token % 256] = job;
    else
        store_uncertain(db, r->slot);
}
static void sim_state(Radio *r, Parcel *p) {
    int state = parcel_int(p);
    parcel_int(p);
    int gsm = parcel_int(p);
    parcel_int(p);
    parcel_int(p);
    int apps = parcel_int(p);
    if (p->error || apps < 0 || apps > 8)
        return;
    r->sim = state;
    if (state != 1)
        r->registered = -1;
    r->active_app = -1;
    int candidate = -1;
    for (int i = 0; i < apps; i++) {
        int type = parcel_int(p), ready = parcel_int(p);
        parcel_int(p);
        char ignored[1024], aid[129];
        if (parcel_string(p, aid, sizeof(aid)) || parcel_string(p, ignored, sizeof(ignored)))
            return;
        parcel_int(p);
        parcel_int(p);
        parcel_int(p);
        if (p->error)
            return;
        if ((type == 1 || type == 2) && ready == 5)
            candidate = i;
        if (i == gsm) {
            r->active_app = ready;
            r->app_type = type;
            strcpy(r->aid, aid);
        }
    }
    if (state == 1 && gsm < 0 && candidate >= 0 && r->radio == 10 && !r->subscription_attempted) {
        unsigned char args[16];
        put(args, r->slot - 1);
        put(args + 4, candidate);
        put(args + 8, r->slot - 1);
        put(args + 12, 1);
        request(r, 122, args, sizeof(args));
        r->subscription_attempted = 1;
    }
}
int radio_handle(Radio *r, sqlite3 *db, const unsigned char *data, size_t size) {
    Parcel p = {data, size, 0, 0};
    int kind = parcel_int(&p), code = parcel_int(&p);
    if (p.error)
        return -1;
    if (kind == 0) {
        int error = parcel_int(&p);
        if (p.error || code < 1 || code >= 1000000000)
            return -1;
        int index = code % 256, request_code = r->pending[index];
        trace(r, "reply", request_code, error);
        int64_t job = r->pending_job[index];
        r->pending_job[index] = 0;
        r->pending[index] = 0;
        if (request_code == 20)
            r->registration_retry_at = monotonic_ms() + 15000;
        if (code == r->control_token) {
            call_reply(r, error);
            request(r, 9, NULL, 0);
        }
        if (request_code == 25 && job) {
            if (!error) {
                char ack_pdu[1024];
                parcel_int(&p);
                if (parcel_string(&p, ack_pdu, sizeof(ack_pdu)))
                    return -1;
                parcel_int(&p);
                if (p.error)
                    return -1;
            }
            return store_sent(db, job, error == 0);
        }
        /* The network's answer arrives separately as RIL_UNSOL_ON_USSD. */
        if (request_code == 29 && code == r->ussd_token) {
            r->ussd_token = 0;
            if (error)
                ussd_set(r, USSD_FAILED, -1, error, "");
        }
        if (request_code == 30 && !error &&
            (r->ussd_state == USSD_WAITING || r->ussd_state == USSD_REPLY))
            ussd_set(r, USSD_CANCELLED, -1, 0, "");
        if (error) {
            if (request_code == 9) {
                r->call_state = -2;
                r->call_index = 0;
            }
            if (request_code == 1)
                r->sim = r->active_app = r->registered = -1;
            if (request_code == 20)
                r->registered = -1;
            return 0;
        }
        if (request_code == 1)
            sim_state(r, &p);
        else if (request_code == 9) {
            int count = parcel_int(&p);
            if (p.error || count < 0 || count > 32)
                return -1;
            int state = count ? parcel_int(&p) : -1;
            int call_index = count ? parcel_int(&p) : 0;
            if (p.error || (count && (state < 0 || state > 5 || call_index < 1 || call_index > 32)))
                return -1;
            /* First call is enough for the status screen; no number is exposed. */
            r->calls = count;
            r->incoming = count && (state == 4 || state == 5);
            r->call_state = state;
            r->call_index = call_index;
        } else if (request_code == 20) {
            int count = parcel_int(&p);
            char state[64];
            if (count > 0 && count < 32 && !parcel_string(&p, state, sizeof(state)))
                r->registered = (!strcmp(state, "1") || !strcmp(state, "5"));
        } else if (request_code == 28) {
            int sw1 = parcel_int(&p);
            parcel_int(&p);
            char record[KF_PDU];
            if ((sw1 == 0x90 || sw1 == 0x91) && !parcel_string(&p, record, sizeof(record)) &&
                strlen(record) > 2 && (record[1] == '1' || record[1] == '3')) {
                if (store_pdu(db, r->slot, record + 2))
                    return -1;
            }
        }
        return p.error ? -1 : 0;
    }
    if (kind != 1)
        return -1;
    trace(r, "event", code, 0);
    if (code == 1000) {
        int state = parcel_int(&p);
        if (p.error)
            return -1;
        r->radio = state;
        if (state == 0)
            ints(r, 23, 1, 1, 0);
        if (state == 10) {
            request(r, 1, NULL, 0);
            request(r, 20, NULL, 0);
        }
    } else if (code == 1034) {
        ints(r, 61, 1, 1, 0);
        ints(r, 102, 1, 1, 0);
        request(r, 1, NULL, 0);
        request(r, 20, NULL, 0);
        request(r, 9, NULL, 0);
    } else if (code == 1001 || code == 1018) {
        request(r, 9, NULL, 0);
    } else if (code == 1002)
        request(r, 20, NULL, 0);
    else if (code == 1006) {
        /* RIL_UNSOL_ON_USSD: type "0".."5", then the decoded text if any. */
        int count = parcel_int(&p);
        char type[8], message[KF_TEXT] = "";
        if (p.error || count < 1 || count > 8 || parcel_string(&p, type, sizeof(type)) ||
            (count > 1 && parcel_string(&p, message, sizeof(message))))
            return -1;
        int t = type[0] >= '0' && type[0] <= '5' && !type[1] ? type[0] - '0' : -1;
        ussd_set(r,
                 t == 1                         ? USSD_REPLY
                 : t == 0 || t == 2 || t == 3 ? USSD_DONE
                                              : USSD_FAILED,
                 t, 0, message);
    }
    else if (code == 1019) {
        r->subscription_attempted = 0;
        request(r, 1, NULL, 0);
    } else if (code == 1003) {
        char pdu[KF_PDU];
        if (parcel_string(&p, pdu, sizeof(pdu)))
            return -1;
        int rc = store_pdu(db, r->slot, pdu);
        ack(r, rc == 0);
        return rc;
    } else if (code == 1004) {
        char pdu[KF_PDU];
        if (parcel_string(&p, pdu, sizeof(pdu)))
            return -1;
        int rc = store_unsupported(db, r->slot, code, pdu);
        ack(r, rc == 0);
        return rc;
    } else if (code == 1005) {
        if (parcel_int(&p) != 1)
            return -1;
        int record = parcel_int(&p);
        if (!p.error)
            sim_record(r, record);
    } else if (code == 1020) {
        char hex[32777];
        if (size > 16388)
            return -1;
        for (size_t i = 0; i < size; i++)
            snprintf(hex + i * 2, 3, "%02x", data[i]);
        int rc = store_unsupported(db, r->slot, code, hex);
        ints(r, 88, 2, rc ? 1 : 0, rc ? 35 : 0);
        return rc;
    }
    return p.error ? -1 : 0;
}
void radio_tick(Radio *r, sqlite3 *db, const char *socket_dir) {
    (void)db;
    int64_t now = monotonic_ms();
    if (r->ussd_state == USSD_WAITING && now - r->ussd_since > 60000)
        ussd_set(r, USSD_FAILED, -1, -2, "");
    if (r->fd >= 0) {
        if (r->control_fd >= 0 && now - r->control_at > 5000)
            call_reply(r, -1);
        if (r->used && now - r->partial_at > 10000) {
            radio_close(r);
            return;
        }
        int sms_pending = 0;
        for (int i = 0; i < 256; i++)
            if (r->pending[i] == 25 && r->pending_job[i])
                sms_pending = 1;
        for (int i = 0; i < 256; i++) {
            /* A delivered SMS can take longer than 30 seconds to acknowledge.
             * Keep its socket/token alive, including when the modem serializes
             * unrelated queries behind it. Never retry the SMS on expiry. */
            if (!r->pending[i] || (sms_pending && r->pending[i] != 25))
                continue;
            int64_t timeout = r->pending[i] == 25 ? 180000 : 30000;
            if (now - r->pending_at[i] > timeout) {
                radio_close(r);
                return;
            }
        }
        return;
    }
    if (now < r->retry_at)
        return;
    r->retry_at = now + 5000;
    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return;
    struct sockaddr_un address = {0};
    address.sun_family = AF_UNIX;
    if (snprintf(address.sun_path, sizeof(address.sun_path), "%s/rild%s", socket_dir,
                 r->slot == 1 ? "" : "2") >= (int)sizeof(address.sun_path)) {
        close(fd);
        return;
    }
    struct timeval timeout = {2, 0};
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) {
        close(fd);
        return;
    }
    r->fd = fd;
    r->last_rx = now;
    trace(r, "connect", 0, 0);
    request(r, 1, NULL, 0);
}
void radio_read(Radio *r, sqlite3 *db) {
    ssize_t n = recv(r->fd, r->buffer + r->used, sizeof(r->buffer) - r->used, MSG_DONTWAIT);
    if (n < 0 && (errno == EAGAIN || errno == EINTR))
        return;
    if (n <= 0) {
        radio_close(r);
        return;
    }
    if (!r->used)
        r->partial_at = monotonic_ms();
    r->used += (size_t)n;
    r->last_rx = monotonic_ms();
    while (r->used >= 4) {
        size_t size = (size_t)r->buffer[0] << 24 | (size_t)r->buffer[1] << 16 |
                      (size_t)r->buffer[2] << 8 | r->buffer[3];
        if (size < 8 || size > 16384) {
            radio_close(r);
            return;
        }
        if (r->used < size + 4)
            return;
        if (radio_handle(r, db, r->buffer + 4, size)) {
            radio_close(r);
            return;
        }
        if (r->fd < 0)
            return;
        r->used -= size + 4;
        memmove(r->buffer, r->buffer + size + 4, r->used);
        r->partial_at = monotonic_ms();
    }
}
