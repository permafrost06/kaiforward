#ifndef KAIFORWARD_NATIVE_H
#define KAIFORWARD_NATIVE_H
#define _GNU_SOURCE
#include <sqlite3.h>
#include <stddef.h>
#include <stdint.h>

#define KF_HOME "/data/kaiforward"
#define KF_TEXT 2048
#define KF_PDU 1024
#define KF_NETWORKS 16
#define KF_USSD 512
enum { USSD_IDLE, USSD_WAITING, USSD_REPLY, USSD_DONE, USSD_FAILED, USSD_CANCELLED };
typedef struct {
    char ssid[33];
    int signal, security; /* 0 open, 1 WPA PSK, 2 unsupported */
} WifiNetwork;
typedef struct {
    char ssid[129], password[129], url[1025], token[257], relay_url[257];
} Config;
typedef struct {
    char sender[129], body[KF_TEXT];
    int64_t timestamp;
    int reference, total, part, encoding;
} Sms;
typedef struct {
    int fd, slot, serial, radio, sim, registered, active_app, calls, incoming;
    int call_state, call_index, control_fd, control_token;
    int64_t control_at;
    unsigned char buffer[16388];
    size_t used;
    int pending[256];
    int64_t pending_job[256];
    int64_t pending_at[256], last_rx, retry_at, partial_at;
    int subscription_attempted;
    int app_type;
    char aid[129];
    /* Latest USSD exchange; one per slot. The id changes on every update. */
    int ussd_state, ussd_type, ussd_error, ussd_token, ussd_id;
    int64_t ussd_since;
    char ussd_message[KF_USSD];
} Radio;
typedef struct {
    const unsigned char *data;
    size_t size, offset;
    int error;
} Parcel;

int permission_allowed(const char *name, int requester, int uid);
int permission_reply(unsigned code, int requester, const unsigned char *data, size_t length,
                     unsigned char *reply, size_t cap);
int permission_run(void);
int64_t now_ms(void);
int64_t monotonic_ms(void);
int atomic_write(const char *path, const char *data);
int service_lock(const char *home, const char *name);
int config_load(const char *home, Config *c);
int config_save(const char *home, const Config *c);
int config_valid(const Config *c);
int relay_address(const char *url, char *host, size_t host_cap, char *port, size_t port_cap);
int settings_read(const char *home, char *json, size_t cap);
int settings_update(sqlite3 *db, const char *home, const char *json);
int settings_key(const char *home, char key[65]);
int settings_command(const char *home, int action);
struct pollfd;
int settings_http_open(const char *home, const char *address, int port);
int settings_http_fds(struct pollfd *fds);
void settings_http_tick(const char *home, sqlite3 *db, const Radio radios[2]);
void settings_http_close(void);
int battery_level(void);
int wifi_connected(void);
const char *radio_label(const Radio *r);
int status_read(const char *home, sqlite3 *db, const Radio radios[2], char *json, size_t cap);
int json_string(char *out, size_t cap, const char *s);
int ack_valid(const char *json, const char *key);
int sms_decode(const char *hex, Sms *s);
int gsm_septets(unsigned cp, unsigned char out[2]);
int sms_submit(const char *recipient, const char *body, char *out, size_t cap);
sqlite3 *store_open(const char *home);
int store_pdu(sqlite3 *db, int slot, const char *pdu);
int store_unsupported(sqlite3 *db, int slot, int type, const char *hex);
int store_count(sqlite3 *db, const char *table, const char *where);
int store_next(sqlite3 *db, char *json, size_t cap, char *key, size_t key_cap,
               int64_t *id);
int store_ack(sqlite3 *db, int64_t id);
int64_t store_send(sqlite3 *db, int slot, const char *recipient, const char *body);
int64_t store_outgoing(sqlite3 *db, int slot, char *pdu, size_t cap);
int store_sent(sqlite3 *db, int64_t job, int success);
int store_jobs(sqlite3 *db, int slot, char *json, size_t cap);
void store_uncertain(sqlite3 *db, int slot);
void radio_send_queued(Radio *r, sqlite3 *db);
int radio_call(Radio *r, int action, const char *number, int client);
int radio_ussd(Radio *r, const char *code, int client);
int ussd_read(const Radio *r, char *json, size_t cap);
int call_listen(const char *home);
void call_accept(int listener, Radio radios[2], sqlite3 *db);
int call_client(const char *home, int slot, const char *action, const char *number);
void call_reply(Radio *r, int result);
void uploader(const char *home, const char *curl);
int parcel_int(Parcel *p);
int parcel_string(Parcel *p, char *out, size_t cap);
void radio_init(Radio *r, int slot);
void radio_trace_home(const char *home);
void radio_tick(Radio *r, sqlite3 *db, const char *socket_dir);
void radio_read(Radio *r, sqlite3 *db);
void radio_close(Radio *r);
int radio_handle(Radio *r, sqlite3 *db, const unsigned char *data, size_t size);
void ui_run(const char *home, const char *preview);
int wifi_parse(char *reply, WifiNetwork *networks, int capacity);
int wifi_scan(const char *home, const char *socket_path);
int wifi_load(const char *home, WifiNetwork *networks, int capacity);
#endif
