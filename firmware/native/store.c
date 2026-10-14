#include "native.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int exec(sqlite3 *db, const char *sql) {
    return sqlite3_exec(db, sql, NULL, NULL, NULL) == SQLITE_OK ? 0 : -1;
}
static int commit(sqlite3 *db) {
    if (!exec(db, "COMMIT"))
        return 0;
    exec(db, "ROLLBACK");
    return -1;
}
static sqlite3_stmt *prepare(sqlite3 *db, const char *sql) {
    sqlite3_stmt *q = NULL;
    if (sqlite3_prepare_v2(db, sql, -1, &q, NULL) != SQLITE_OK)
        return NULL;
    return q;
}
static void text(sqlite3_stmt *q, int n, const char *s) {
    sqlite3_bind_text(q, n, s, -1, SQLITE_TRANSIENT);
}
sqlite3 *store_open(const char *home) {
    char path[1200];
    snprintf(path, sizeof(path), "%s/native.sqlite3", home);
    sqlite3 *db = NULL;
    if (sqlite3_open(path, &db) != SQLITE_OK) {
        sqlite3_close(db);
        return NULL;
    }
    sqlite3_busy_timeout(db, 5000);
    if (exec(db, "PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;"
                 "CREATE TABLE IF NOT EXISTS raw(id INTEGER PRIMARY KEY,slot INTEGER,type "
                 "INTEGER,pdu TEXT,decoded INTEGER DEFAULT 0,UNIQUE(slot,type,pdu));"
                 "CREATE TABLE IF NOT EXISTS groups(id INTEGER PRIMARY KEY,slot INTEGER,sender "
                 "TEXT,reference INTEGER,total INTEGER,encoding INTEGER,stamp INTEGER,complete "
                 "INTEGER DEFAULT 0);"
                 "CREATE TABLE IF NOT EXISTS parts(group_id INTEGER,part INTEGER,body TEXT,raw_id "
                 "INTEGER,PRIMARY KEY(group_id,part));"
                 "CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,stamp "
                 "INTEGER,sender TEXT,body TEXT,slot INTEGER,uploaded INTEGER DEFAULT 0,direction "
                 "TEXT DEFAULT 'received',recipient TEXT DEFAULT '');"
                 "CREATE TABLE IF NOT EXISTS outbox(id INTEGER PRIMARY KEY AUTOINCREMENT,slot "
                 "INTEGER,recipient TEXT,body TEXT,pdu TEXT,stamp INTEGER,status TEXT DEFAULT "
                 "'queued');")) {
        sqlite3_close(db);
        return NULL;
    }
    /* Upgrade queues created by the first experimental native build. */
    sqlite3_stmt *columns = prepare(db, "PRAGMA table_info(messages)");
    int direction = 0, recipient = 0;
    if (!columns) {
        sqlite3_close(db);
        return NULL;
    }
    while (sqlite3_step(columns) == SQLITE_ROW) {
        const char *name = (const char *)sqlite3_column_text(columns, 1);
        if (!strcmp(name, "direction"))
            direction = 1;
        if (!strcmp(name, "recipient"))
            recipient = 1;
    }
    sqlite3_finalize(columns);
    if ((!direction &&
         exec(db, "ALTER TABLE messages ADD COLUMN direction TEXT DEFAULT 'received'")) ||
        (!recipient && exec(db, "ALTER TABLE messages ADD COLUMN recipient TEXT DEFAULT ''"))) {
        sqlite3_close(db);
        return NULL;
    }
    return db;
}
int store_unsupported(sqlite3 *db, int slot, int type, const char *hex) {
    sqlite3_stmt *q = prepare(db, "INSERT OR IGNORE INTO raw(slot,type,pdu) VALUES(?,?,?)");
    if (!q)
        return -1;
    sqlite3_bind_int(q, 1, slot);
    sqlite3_bind_int(q, 2, type);
    text(q, 3, hex);
    int rc = sqlite3_step(q);
    sqlite3_finalize(q);
    return rc == SQLITE_DONE ? 0 : -1;
}
int store_pdu(sqlite3 *db, int slot, const char *pdu) {
    Sms sms;
    int decoded = sms_decode(pdu, &sms) == 0;
    char *body = NULL;
    if (exec(db, "BEGIN IMMEDIATE"))
        return -1;
    sqlite3_stmt *q = prepare(db, "INSERT OR IGNORE INTO raw(slot,type,pdu) VALUES(?,1003,?)");
    if (!q)
        goto fail;
    sqlite3_bind_int(q, 1, slot);
    text(q, 2, pdu);
    int rc = sqlite3_step(q);
    sqlite3_finalize(q);
    if (rc != SQLITE_DONE)
        goto fail;
    if (!sqlite3_changes(db) || !decoded)
        return commit(db);
    int64_t raw_id = sqlite3_last_insert_rowid(db), group_id = 0;
    if (sms.total > 1) {
        /* Match only incomplete groups within ten minutes of the service-centre timestamp. */
        q = prepare(
            db, "SELECT id FROM groups WHERE slot=? AND sender=? AND reference=? AND total=? AND "
                "encoding=? AND complete=0 AND abs(stamp-?)<=600000 ORDER BY id DESC LIMIT 1");
        if (!q)
            goto fail;
        sqlite3_bind_int(q, 1, slot);
        text(q, 2, sms.sender);
        sqlite3_bind_int(q, 3, sms.reference);
        sqlite3_bind_int(q, 4, sms.total);
        sqlite3_bind_int(q, 5, sms.encoding);
        sqlite3_bind_int64(q, 6, sms.timestamp);
        if (sqlite3_step(q) == SQLITE_ROW)
            group_id = sqlite3_column_int64(q, 0);
        sqlite3_finalize(q);
        if (!group_id) {
            q = prepare(db, "INSERT INTO groups(slot,sender,reference,total,encoding,stamp) "
                            "VALUES(?,?,?,?,?,?)");
            if (!q)
                goto fail;
            sqlite3_bind_int(q, 1, slot);
            text(q, 2, sms.sender);
            sqlite3_bind_int(q, 3, sms.reference);
            sqlite3_bind_int(q, 4, sms.total);
            sqlite3_bind_int(q, 5, sms.encoding);
            sqlite3_bind_int64(q, 6, sms.timestamp);
            rc = sqlite3_step(q);
            sqlite3_finalize(q);
            if (rc != SQLITE_DONE)
                goto fail;
            group_id = sqlite3_last_insert_rowid(db);
        }
        q = prepare(db, "SELECT body FROM parts WHERE group_id=? AND part=?");
        if (!q)
            goto fail;
        sqlite3_bind_int64(q, 1, group_id);
        sqlite3_bind_int(q, 2, sms.part);
        rc = sqlite3_step(q);
        if (rc == SQLITE_ROW) {
            int same = !strcmp((const char *)sqlite3_column_text(q, 0), sms.body);
            sqlite3_finalize(q);
            /* Conflicting segments stay raw for inspection rather than producing corrupt text. */
            if (!same)
                return commit(db);
        } else {
            sqlite3_finalize(q);
            if (rc != SQLITE_DONE)
                goto fail;
            q = prepare(db, "INSERT INTO parts(group_id,part,body,raw_id) VALUES(?,?,?,?)");
            if (!q)
                goto fail;
            sqlite3_bind_int64(q, 1, group_id);
            sqlite3_bind_int(q, 2, sms.part);
            text(q, 3, sms.body);
            sqlite3_bind_int64(q, 4, raw_id);
            rc = sqlite3_step(q);
            sqlite3_finalize(q);
            if (rc != SQLITE_DONE)
                goto fail;
        }
        q = prepare(db, "UPDATE raw SET decoded=1 WHERE id=?");
        if (!q)
            goto fail;
        sqlite3_bind_int64(q, 1, raw_id);
        rc = sqlite3_step(q);
        sqlite3_finalize(q);
        if (rc != SQLITE_DONE)
            goto fail;
        q = prepare(db, "SELECT COUNT(*),COALESCE(SUM(length(CAST(body AS BLOB))),0) FROM parts "
                        "WHERE group_id=?");
        if (!q)
            goto fail;
        sqlite3_bind_int64(q, 1, group_id);
        if (sqlite3_step(q) != SQLITE_ROW) {
            sqlite3_finalize(q);
            goto fail;
        }
        int count = sqlite3_column_int(q, 0), bytes = sqlite3_column_int(q, 1);
        sqlite3_finalize(q);
        if (count != sms.total || bytes > 65536)
            return commit(db);
        body = calloc((size_t)bytes + 1, 1);
        if (!body)
            goto fail;
        q = prepare(db, "SELECT body FROM parts WHERE group_id=? ORDER BY part");
        if (!q)
            goto fail;
        sqlite3_bind_int64(q, 1, group_id);
        while ((rc = sqlite3_step(q)) == SQLITE_ROW)
            strcat(body, (const char *)sqlite3_column_text(q, 0));
        sqlite3_finalize(q);
        if (rc != SQLITE_DONE)
            goto fail;
        q = prepare(db, "SELECT stamp FROM groups WHERE id=?");
        if (!q)
            goto fail;
        sqlite3_bind_int64(q, 1, group_id);
        if (sqlite3_step(q) != SQLITE_ROW) {
            sqlite3_finalize(q);
            goto fail;
        }
        sms.timestamp = sqlite3_column_int64(q, 0);
        sqlite3_finalize(q);
    }
    q = prepare(db, "INSERT INTO messages(stamp,sender,body,slot) VALUES(?,?,?,?)");
    if (!q)
        goto fail;
    sqlite3_bind_int64(q, 1, sms.timestamp);
    text(q, 2, sms.sender);
    text(q, 3, body ? body : sms.body);
    sqlite3_bind_int(q, 4, slot);
    rc = sqlite3_step(q);
    sqlite3_finalize(q);
    free(body);
    body = NULL;
    if (rc != SQLITE_DONE)
        goto fail;
    if (group_id) {
        q = prepare(db, "UPDATE groups SET complete=1 WHERE id=?");
        if (!q)
            goto fail;
        sqlite3_bind_int64(q, 1, group_id);
        rc = sqlite3_step(q);
        sqlite3_finalize(q);
        if (rc != SQLITE_DONE)
            goto fail;
    }
    q = prepare(db, "UPDATE raw SET decoded=1 WHERE id=?");
    if (!q)
        goto fail;
    sqlite3_bind_int64(q, 1, raw_id);
    rc = sqlite3_step(q);
    sqlite3_finalize(q);
    if (rc != SQLITE_DONE)
        goto fail;
    return commit(db);
fail:
    free(body);
    exec(db, "ROLLBACK");
    return -1;
}
int store_count(sqlite3 *db, const char *table, const char *where) {
    char sql[256];
    snprintf(sql, sizeof(sql), "SELECT COUNT(*) FROM %s WHERE %s", table, where);
    sqlite3_stmt *q = prepare(db, sql);
    if (!q)
        return -1;
    int count = -1;
    if (sqlite3_step(q) == SQLITE_ROW)
        count = sqlite3_column_int(q, 0);
    sqlite3_finalize(q);
    return count;
}
int store_next(sqlite3 *db, char *json, size_t cap, char *key, size_t key_cap,
               int64_t *id) {
    sqlite3_stmt *q = prepare(db, "SELECT id,stamp,sender,body,slot,direction,recipient FROM "
                                  "messages WHERE uploaded=0 ORDER BY id LIMIT 1");
    if (!q)
        return -1;
    int rc = sqlite3_step(q);
    if (rc == SQLITE_DONE) {
        sqlite3_finalize(q);
        return 0;
    }
    if (rc != SQLITE_ROW) {
        sqlite3_finalize(q);
        return -1;
    }
    *id = sqlite3_column_int64(q, 0);
    int64_t stamp = sqlite3_column_int64(q, 1), wire_id = 1000000000 + *id;
    snprintf(key, key_cap, "%lld:%lld", (long long)wire_id, (long long)stamp);
    char sender[800], recipient[800], direction[100];
    const char *s = (const char *)sqlite3_column_text(q, 3);
    char *body = malloc(strlen(s) * 6 + 3);
    if (!body) {
        sqlite3_finalize(q);
        return -1;
    }
    int bad =
        json_string(sender, sizeof(sender), (const char *)sqlite3_column_text(q, 2)) < 0 ||
        json_string(body, strlen(s) * 6 + 3, s) < 0 ||
        json_string(recipient, sizeof(recipient), (const char *)sqlite3_column_text(q, 6)) < 0 ||
        json_string(direction, sizeof(direction), (const char *)sqlite3_column_text(q, 5)) < 0;
    int length = -1;
    if (!bad)
        length = snprintf(json, cap,
                          "{\"messages\":[{\"key\":\"%s\",\"id\":%lld,"
                          "\"timestamp\":%lld,\"sender\":%s,\"recipient\":%s,\"direction\":%s,"
                          "\"body\":%s,\"sim_slot\":%d}]}",
                          key, (long long)wire_id, (long long)stamp, sender, recipient,
                          direction, body, sqlite3_column_int(q, 4));
    free(body);
    sqlite3_finalize(q);
    return length < 0 || (size_t)length >= cap ? -1 : 1;
}
int store_ack(sqlite3 *db, int64_t id) {
    sqlite3_stmt *q = prepare(db, "UPDATE messages SET uploaded=1 WHERE id=?");
    if (!q)
        return -1;
    sqlite3_bind_int64(q, 1, id);
    int rc = sqlite3_step(q);
    sqlite3_finalize(q);
    return rc == SQLITE_DONE ? 0 : -1;
}

int64_t store_send(sqlite3 *db, int slot, const char *recipient, const char *body) {
    char pdu[400];
    if ((slot != 1 && slot != 2) || sms_submit(recipient, body, pdu, sizeof(pdu)))
        return -1;
    sqlite3_stmt *q =
        prepare(db, "INSERT INTO outbox(slot,recipient,body,pdu,stamp) VALUES(?,?,?,?,?)");
    if (!q)
        return -1;
    sqlite3_bind_int(q, 1, slot);
    text(q, 2, recipient);
    text(q, 3, body);
    text(q, 4, pdu);
    sqlite3_bind_int64(q, 5, now_ms());
    int rc = sqlite3_step(q);
    sqlite3_finalize(q);
    return rc == SQLITE_DONE ? sqlite3_last_insert_rowid(db) : -1;
}
int64_t store_outgoing(sqlite3 *db, int slot, char *pdu, size_t cap) {
    if (exec(db, "BEGIN IMMEDIATE"))
        return -1;
    sqlite3_stmt *q = prepare(
        db, "SELECT id,pdu FROM outbox WHERE slot=? AND status='queued' ORDER BY id LIMIT 1");
    if (!q)
        goto fail;
    sqlite3_bind_int(q, 1, slot);
    int rc = sqlite3_step(q);
    if (rc == SQLITE_DONE) {
        sqlite3_finalize(q);
        return commit(db) ? -1 : 0;
    }
    if (rc != SQLITE_ROW) {
        sqlite3_finalize(q);
        goto fail;
    }
    int64_t job = sqlite3_column_int64(q, 0);
    const char *value = (const char *)sqlite3_column_text(q, 1);
    if (strlen(value) >= cap) {
        sqlite3_finalize(q);
        goto fail;
    }
    strcpy(pdu, value);
    sqlite3_finalize(q);
    q = prepare(db, "UPDATE outbox SET status='submitting' WHERE id=?");
    if (!q)
        goto fail;
    sqlite3_bind_int64(q, 1, job);
    rc = sqlite3_step(q);
    sqlite3_finalize(q);
    if (rc != SQLITE_DONE)
        goto fail;
    return commit(db) ? -1 : job;
fail:
    exec(db, "ROLLBACK");
    return -1;
}
int store_sent(sqlite3 *db, int64_t job, int success) {
    if (exec(db, "BEGIN IMMEDIATE"))
        return -1;
    sqlite3_stmt *q;
    if (success) {
        q = prepare(
            db,
            "INSERT INTO messages(stamp,sender,body,slot,direction,recipient) SELECT "
            "stamp,'',body,slot,'sent',recipient FROM outbox WHERE id=? AND status='submitting'");
        if (!q)
            goto fail;
        sqlite3_bind_int64(q, 1, job);
        int rc = sqlite3_step(q);
        sqlite3_finalize(q);
        if (rc != SQLITE_DONE)
            goto fail;
    }
    q = prepare(db, "UPDATE outbox SET status=? WHERE id=? AND status='submitting'");
    if (!q)
        goto fail;
    text(q, 1, success ? "sent" : "failed");
    sqlite3_bind_int64(q, 2, job);
    int rc = sqlite3_step(q);
    sqlite3_finalize(q);
    if (rc != SQLITE_DONE)
        goto fail;
    return commit(db);
fail:
    exec(db, "ROLLBACK");
    return -1;
}
void store_uncertain(sqlite3 *db, int slot) {
    sqlite3_stmt *q = prepare(
        db, "UPDATE outbox SET status='uncertain' WHERE status='submitting' AND (?=0 OR slot=?)");
    if (!q)
        return;
    sqlite3_bind_int(q, 1, slot);
    sqlite3_bind_int(q, 2, slot);
    sqlite3_step(q);
    sqlite3_finalize(q);
}
