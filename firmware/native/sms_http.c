#include "native.h"
#include <stdio.h>
#include <string.h>

static int request_id_valid(const char *id) {
    if (strlen(id) != 36)
        return 0;
    for (int i = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (id[i] != '-')
                return 0;
        } else if (!strchr("0123456789abcdef", id[i]))
            return 0;
    }
    return 1;
}

/* Return only job metadata, never message contents or recipients. */
static int read_job(sqlite3 *db, const char *id, char *json, size_t cap) {
    sqlite3_stmt *q = NULL;
    if (sqlite3_prepare_v2(db, "SELECT o.id,o.slot,o.status FROM sms_requests r JOIN outbox o "
                             "ON o.id=r.job WHERE r.request_id=?", -1, &q, NULL) != SQLITE_OK)
        return 500;
    sqlite3_bind_text(q, 1, id, -1, SQLITE_TRANSIENT);
    int rc = sqlite3_step(q), result = rc == SQLITE_DONE ? 404 : 500;
    if (rc == SQLITE_ROW) {
        char status[128];
        if (json_string(status, sizeof(status), (const char *)sqlite3_column_text(q, 2)) >= 0) {
            int n = snprintf(json, cap, "{\"job\":%lld,\"slot\":%d,\"status\":%s}\n",
                             (long long)sqlite3_column_int64(q, 0), sqlite3_column_int(q, 1), status);
            result = n >= 0 && (size_t)n < cap ? 200 : 500;
        }
    }
    sqlite3_finalize(q);
    return result;
}

static int submit(sqlite3 *db, const char *body, char *json, size_t cap) {
    sqlite3_stmt *q = NULL;
    char id[37] = "", number[22] = "", text[641] = "", pdu[400];
    int slot = 0, result = 400, rc;
    unsigned seen = 0;
    if (sqlite3_prepare_v2(db, "SELECT CASE WHEN json_valid(?) THEN json_type(?) END", -1, &q, NULL) != SQLITE_OK)
        return 500;
    sqlite3_bind_text(q, 1, body, -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(q, 2, body, -1, SQLITE_TRANSIENT);
    int object = sqlite3_step(q) == SQLITE_ROW && sqlite3_column_type(q, 0) == SQLITE_TEXT &&
                 !strcmp((const char *)sqlite3_column_text(q, 0), "object");
    sqlite3_finalize(q);
    q = NULL;
    if (!object)
        return 400;
    if (sqlite3_prepare_v2(db, "SELECT key,type,value FROM json_each(?)", -1, &q, NULL) != SQLITE_OK)
        return 500;
    sqlite3_bind_text(q, 1, body, -1, SQLITE_TRANSIENT);
    while ((rc = sqlite3_step(q)) == SQLITE_ROW) {
        const char *name = (const char *)sqlite3_column_text(q, 0);
        const char *type = (const char *)sqlite3_column_text(q, 1);
        if ((size_t)sqlite3_column_bytes(q, 0) != strlen(name))
            goto done;
        unsigned bit = !strcmp(name, "requestId") ? 1 : !strcmp(name, "slot") ? 2
                       : !strcmp(name, "number") ? 4 : !strcmp(name, "text") ? 8 : 0;
        if (!bit || (seen & bit))
            goto done;
        seen |= bit;
        if (bit == 2) {
            sqlite3_int64 value = sqlite3_column_int64(q, 2);
            if (strcmp(type, "integer") || (value != 1 && value != 2))
                goto done;
            slot = (int)value;
        } else {
            const char *value = (const char *)sqlite3_column_text(q, 2);
            int length = sqlite3_column_bytes(q, 2);
            char *dest = bit == 1 ? id : bit == 4 ? number : text;
            size_t size = bit == 1 ? sizeof(id) : bit == 4 ? sizeof(number) : sizeof(text);
            if (strcmp(type, "text") || length < 0 || (size_t)length >= size || memchr(value, 0, (size_t)length))
                goto done;
            memcpy(dest, value, (size_t)length);
            dest[length] = 0;
        }
    }
    if (rc != SQLITE_DONE) {
        result = 500;
        goto done;
    }
    if (seen != 15 || !request_id_valid(id) || sms_submit(number, text, pdu, sizeof(pdu)))
        goto done;
    sqlite3_finalize(q);
    q = NULL;
    if (sqlite3_exec(db, "BEGIN IMMEDIATE", NULL, NULL, NULL) != SQLITE_OK)
        return 409;
    result = 500;
    if (sqlite3_prepare_v2(db, "SELECT o.slot,o.recipient,o.body FROM sms_requests r JOIN outbox o "
                             "ON o.id=r.job WHERE r.request_id=?", -1, &q, NULL) != SQLITE_OK)
        goto rollback;
    sqlite3_bind_text(q, 1, id, -1, SQLITE_TRANSIENT);
    rc = sqlite3_step(q);
    if (rc == SQLITE_ROW) {
        int same = sqlite3_column_int(q, 0) == slot &&
                   !strcmp((const char *)sqlite3_column_text(q, 1), number) &&
                   !strcmp((const char *)sqlite3_column_text(q, 2), text);
        sqlite3_finalize(q);
        q = NULL;
        if (!same) {
            result = 409;
            goto rollback;
        }
    } else if (rc == SQLITE_DONE) {
        sqlite3_finalize(q);
        q = NULL;
        int64_t job = store_send(db, slot, number, text);
        if (job < 0 || sqlite3_prepare_v2(db, "INSERT INTO sms_requests(request_id,job) VALUES(?,?)", -1, &q, NULL) != SQLITE_OK)
            goto rollback;
        sqlite3_bind_text(q, 1, id, -1, SQLITE_TRANSIENT);
        sqlite3_bind_int64(q, 2, job);
        if (sqlite3_step(q) != SQLITE_DONE)
            goto rollback;
        sqlite3_finalize(q);
        q = NULL;
    } else
        goto rollback;
    if (sqlite3_exec(db, "COMMIT", NULL, NULL, NULL) != SQLITE_OK)
        goto rollback;
    result = read_job(db, id, json, cap);
    return result == 200 ? 202 : result;
rollback:
    sqlite3_finalize(q);
    sqlite3_exec(db, "ROLLBACK", NULL, NULL, NULL);
    return result;
done:
    sqlite3_finalize(q);
    return result;
}

int sms_http_request(sqlite3 *db, const char *method, const char *path, const char *body,
                     char *json, size_t cap) {
    if (!strcmp(path, "/v1/sms")) {
        if (strcmp(method, "GET"))
            return 405;
        int n = snprintf(json, cap, "{\"slots\":[1,2]}\n");
        return n >= 0 && (size_t)n < cap ? 200 : 500;
    }
    if (!strcmp(path, "/v1/sms/send"))
        return !strcmp(method, "POST") ? submit(db, body, json, cap) : 405;
    const char *prefix = "/v1/sms/requests/";
    if (!strncmp(path, prefix, strlen(prefix)) && request_id_valid(path + strlen(prefix)))
        return !strcmp(method, "GET") ? read_job(db, path + strlen(prefix), json, cap) : 405;
    return 404;
}
