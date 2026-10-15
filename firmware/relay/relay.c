#define _GNU_SOURCE
#include "../native/native.h"
#include "pcm.h"
#include <errno.h>
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>
#include <poll.h>
#include <psa/crypto.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

enum { HELLO = 1, STATE, DOWNLINK, COMMAND, RESULT, START, STOP, UPLINK, PING };
static volatile sig_atomic_t running = 1;
static void stop(int sig) {
    (void)sig;
    running = 0;
}
static uint32_t get32(const unsigned char *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}
static void put32(unsigned char *p, uint32_t value) {
    for (int i = 0; i < 4; i++)
        p[i] = (unsigned char)(value >> ((3 - i) * 8));
}
static int send_frame(mbedtls_ssl_context *ssl, int fd, int kind, const void *body, size_t size) {
    if (size > 8191)
        return -1;
    unsigned char frame[8196];
    put32(frame, (uint32_t)size + 1);
    frame[4] = (unsigned char)kind;
    if (size)
        memcpy(frame + 5, body, size);
    size_t sent = 0;
    int64_t end = monotonic_ms() + 2000;
    while (sent < size + 5 && running && monotonic_ms() < end) {
        int n = mbedtls_ssl_write(ssl, frame + sent, size + 5 - sent);
        if (n > 0) {
            sent += (size_t)n;
            continue;
        }
        if (n != MBEDTLS_ERR_SSL_WANT_READ && n != MBEDTLS_ERR_SSL_WANT_WRITE)
            return -1;
        struct pollfd p = {fd, n == MBEDTLS_ERR_SSL_WANT_READ ? POLLIN : POLLOUT, 0};
        poll(&p, 1, 50);
    }
    return sent == size + 5 ? 0 : -1;
}
static int control(const char *home, const char *command, char *reply, size_t cap) {
    int fd = socket(AF_UNIX, SOCK_SEQPACKET | SOCK_CLOEXEC, 0);
    if (fd < 0)
        return -1;
    struct sockaddr_un a = {0};
    a.sun_family = AF_UNIX;
    struct timeval timeout = {6, 0};
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    size_t length = strlen(command);
    if (snprintf(a.sun_path, sizeof(a.sun_path), "%s/call.sock", home) >= (int)sizeof(a.sun_path) ||
        connect(fd, (struct sockaddr *)&a, sizeof(a)) ||
        send(fd, command, length, MSG_NOSIGNAL) != (ssize_t)length) {
        close(fd);
        return -1;
    }
    ssize_t n = recv(fd, reply, cap - 1, MSG_TRUNC);
    close(fd);
    if (n <= 0 || n >= (ssize_t)cap)
        return -1;
    reply[n] = 0;
    while (n > 0 && (reply[n - 1] == '\r' || reply[n - 1] == '\n'))
        reply[--n] = 0;
    return 0;
}
static int session(const char *home, const char *override, const char *port_override,
                   const char *ca_override) {
    Config config;
    if (config_load(home, &config))
        return -1;
    /* An empty relay URL disables the relay unless --server is given. */
    char host[256], port[8] = "8769", ca_file[1200];
    if (relay_address(config.relay_url, host, sizeof(host), port, sizeof(port)) && !override)
        return -1;
    if ((override && strlen(override) >= sizeof(host)) ||
        (port_override && strlen(port_override) >= sizeof(port)))
        return -1;
    if (override)
        strcpy(host, override);
    if (port_override)
        strcpy(port, port_override);
    /* A private relay CA must not replace the public bundle used for SMS uploads. */
    if (ca_override)
        snprintf(ca_file, sizeof(ca_file), "%s", ca_override);
    else {
        snprintf(ca_file, sizeof(ca_file), "%s/relay-ca.pem", home);
        if (access(ca_file, R_OK))
            snprintf(ca_file, sizeof(ca_file), "%s/ca.pem", home);
        if (access(ca_file, R_OK))
            snprintf(ca_file, sizeof(ca_file), "/system/kaiforward/ca.pem");
    }
    mbedtls_net_context network;
    mbedtls_net_init(&network);
    mbedtls_ssl_context ssl;
    mbedtls_ssl_init(&ssl);
    mbedtls_ssl_config options;
    mbedtls_ssl_config_init(&options);
    mbedtls_entropy_context entropy;
    mbedtls_entropy_init(&entropy);
    mbedtls_ctr_drbg_context random;
    mbedtls_ctr_drbg_init(&random);
    mbedtls_x509_crt certificates;
    mbedtls_x509_crt_init(&certificates);
    CallAudio audio = {.capture = -1, .playback = -1};
    int rc = -1, mode = 0, media_slot = 0, authenticated = 0;
    char error[96] = "";
    const unsigned char label[] = "KaiForward native call relay";
    if (psa_crypto_init() != PSA_SUCCESS ||
        mbedtls_ctr_drbg_seed(&random, mbedtls_entropy_func, &entropy, label, sizeof(label) - 1) ||
        mbedtls_x509_crt_parse_file(&certificates, ca_file) ||
        mbedtls_ssl_config_defaults(&options, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                    MBEDTLS_SSL_PRESET_DEFAULT))
        goto cleanup;
    mbedtls_ssl_conf_authmode(&options, MBEDTLS_SSL_VERIFY_REQUIRED);
    mbedtls_ssl_conf_ca_chain(&options, &certificates, NULL);
    mbedtls_ssl_conf_rng(&options, mbedtls_ctr_drbg_random, &random);
    if (mbedtls_ssl_setup(&ssl, &options) || mbedtls_ssl_set_hostname(&ssl, host) ||
        mbedtls_net_connect(&network, host, port, MBEDTLS_NET_PROTO_TCP))
        goto cleanup;
    struct timeval timeout = {5, 0};
    setsockopt(network.fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(network.fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    mbedtls_ssl_set_bio(&ssl, &network, mbedtls_net_send, mbedtls_net_recv, NULL);
    if (mbedtls_ssl_handshake(&ssl) || mbedtls_ssl_get_verify_result(&ssl) ||
        mbedtls_net_set_nonblock(&network))
        goto cleanup;
    char token[1600], hello[1700];
    if (json_string(token, sizeof(token), config.token) < 0)
        goto cleanup;
    snprintf(hello, sizeof(hello), "{\"protocol\":1,\"token\":%s}", token);
    if (send_frame(&ssl, network.fd, HELLO, hello, strlen(hello)))
        goto cleanup;
    unsigned char incoming[8196];
    size_t used = 0;
    int64_t next_status = 0, last_receive = monotonic_ms();
    char statuses[2][512] = {"{\"slot\":1,\"connected\":false}",
                             "{\"slot\":2,\"connected\":false}"};
    char ussd[2][KF_USSD * 6 + 160], sms[2][600];
    while (running) {
        if (monotonic_ms() - last_receive > 8000)
            goto cleanup;
        if (authenticated && monotonic_ms() >= next_status) {
            /* Reconnect with the new destination after a settings change. */
            Config latest;
            if (config_load(home, &latest) || strcmp(latest.relay_url, config.relay_url) ||
                strcmp(latest.token, config.token))
                goto cleanup;
            for (int i = 0; i < 2; i++) {
                char command[24];
                snprintf(command, sizeof(command), "%d status", i + 1);
                if (control(home, command, statuses[i], sizeof(statuses[i])))
                    snprintf(statuses[i], sizeof(statuses[i]),
                             "{\"slot\":%d,\"connected\":false,\"calls\":0,\"state\":-2}", i + 1);
                snprintf(command, sizeof(command), "%d ussd-read", i + 1);
                /* An older SMS service refuses ussd-read; report no USSD. */
                static const char supported[] = "{\"ok\":true,";
                if (control(home, command, ussd[i], sizeof(ussd[i])) ||
                    strncmp(ussd[i], supported, sizeof(supported) - 1))
                    strcpy(ussd[i], "null");
                snprintf(command, sizeof(command), "%d sms-read", i + 1);
                if (control(home, command, sms[i], sizeof(sms[i])) ||
                    strncmp(sms[i], supported, sizeof(supported) - 1))
                    strcpy(sms[i], "null");
            }
            int any_call = 0;
            for (int i = 0; i < 2; i++)
                if (strstr(statuses[i], "\"calls\":1") &&
                    (strstr(statuses[i], "\"state\":0") || strstr(statuses[i], "\"state\":1") ||
                     strstr(statuses[i], "\"state\":2") || strstr(statuses[i], "\"state\":3")))
                    any_call = 1;
            if (mode != any_call) {
                if (audio_mode(any_call))
                    strcpy(error, "Phone audio mode could not be configured");
                else
                    mode = any_call;
            }
            if (media_slot && (!strstr(statuses[media_slot - 1], "\"calls\":1") ||
                               !strstr(statuses[media_slot - 1], "\"state\":0"))) {
                audio_stop(&audio);
                media_slot = 0;
                *error = 0;
            }
            if (audio.capture_pid > 0 && !audio_alive(&audio)) {
                audio_stop(&audio);
                strcpy(error, "Phone audio stream stopped");
            }
            char report[8000];
            const char *media = audio.capture_pid > 0 ? "running" : *error ? "error" : "idle";
            int length = snprintf(report, sizeof(report),
                                  "{\"slots\":[%s,%s],\"media\":\"%s\",\"audio_error\":\"%s\","
                                  "\"ussd\":[%s,%s],\"sms\":[%s,%s]}",
                                  statuses[0], statuses[1], media, error, ussd[0], ussd[1],
                                  sms[0], sms[1]);
            if (length < 0 || length >= (int)sizeof(report))
                snprintf(report, sizeof(report),
                         "{\"slots\":[%s,%s],\"media\":\"%s\",\"audio_error\":\"%s\"}",
                         statuses[0], statuses[1], media, error);
            if (send_frame(&ssl, network.fd, STATE, report, strlen(report)))
                goto cleanup;
            next_status = monotonic_ms() + 1000;
        }
        struct pollfd fds[2] = {{network.fd, POLLIN, 0}, {audio.capture, POLLIN, 0}};
        if (!mbedtls_ssl_check_pending(&ssl) && poll(fds, 2, 20) < 0 && errno != EINTR)
            goto cleanup;
        if (audio.capture >= 0 && fds[1].revents & POLLIN) {
            unsigned char packet[320], newest[320];
            ssize_t n;
            int have = 0;
            while ((n = read(audio.capture, packet, sizeof(packet))) == sizeof(packet)) {
                memcpy(newest, packet, sizeof(packet));
                have = 1;
            }
            if (n > 0 && n != sizeof(packet))
                goto cleanup;
            if (have && send_frame(&ssl, network.fd, DOWNLINK, newest, sizeof(newest)))
                goto cleanup;
        }
        int n = mbedtls_ssl_read(&ssl, incoming + used, sizeof(incoming) - used);
        if (n == MBEDTLS_ERR_SSL_WANT_READ || n == MBEDTLS_ERR_SSL_WANT_WRITE)
            continue;
        if (n <= 0)
            goto cleanup;
        last_receive = monotonic_ms();
        used += (size_t)n;
        while (used >= 4) {
            uint32_t length = get32(incoming);
            if (!length || length > 8192)
                goto cleanup;
            if (used < length + 4)
                break;
            int kind = incoming[4];
            const unsigned char *body = incoming + 5;
            size_t size = length - 1;
            if (!authenticated) {
                if (kind != HELLO || size != 11 || memcmp(body, "{\"ok\":true}", 11))
                    goto cleanup;
                authenticated = 1;
                puts("Native call relay connected over verified TLS.");
                fflush(stdout);
            } else if (kind == COMMAND) {
                char command[512], response[512];
                if (size < 5 || size > 4 + sizeof(command) - 1 || memchr(body + 4, 0, size - 4))
                    goto cleanup;
                memcpy(command, body + 4, size - 4);
                command[size - 4] = 0;
                /* Match the action itself, not SMS text that mentions it. */
                if (!strcmp(command + 1, " hangup")) {
                    audio_stop(&audio);
                    media_slot = 0;
                    *error = 0;
                }
                if (control(home, command, response, sizeof(response)))
                    strcpy(response,
                           "{\"ok\":false,\"uncertain\":true,\"error\":\"Call result unknown\"}");
                unsigned char result[600];
                memcpy(result, body, 4);
                memcpy(result + 4, response, strlen(response));
                if (send_frame(&ssl, network.fd, RESULT, result, 4 + strlen(response)))
                    goto cleanup;
                next_status = 0;
            } else if (kind == START) {
                if (size != 1 || body[0] < 1 || body[0] > 2)
                    goto cleanup;
                int slot = body[0];
                if (!audio.capture_pid && mode && strstr(statuses[slot - 1], "\"calls\":1") &&
                    strstr(statuses[slot - 1], "\"state\":0")) {
                    media_slot = slot;
                    if (audio_start(&audio, network.fd))
                        strcpy(error, "Phone call audio is unavailable or its routes are busy");
                    else
                        *error = 0;
                    next_status = 0;
                }
            } else if (kind == STOP) {
                if (size)
                    goto cleanup;
                audio_stop(&audio);
                media_slot = 0;
                *error = 0;
                next_status = 0;
            } else if (kind == UPLINK) {
                if (size != 320)
                    goto cleanup;
                if (audio.playback >= 0) {
                    ssize_t written = write(audio.playback, body, size);
                    if (written != (ssize_t)size && errno != EAGAIN && errno != EINTR)
                        goto cleanup;
                }
            } else if (kind != PING)
                goto cleanup;
            used -= length + 4;
            memmove(incoming, incoming + length + 4, used);
        }
    }
    rc = 0;
cleanup:
    audio_stop(&audio);
    if (mode)
        audio_mode(0);
    mbedtls_net_free(&network);
    mbedtls_ssl_free(&ssl);
    mbedtls_ssl_config_free(&options);
    mbedtls_x509_crt_free(&certificates);
    mbedtls_ctr_drbg_free(&random);
    mbedtls_entropy_free(&entropy);
    return rc;
}
int main(int argc, char **argv) {
    const char *home = KF_HOME, *server = NULL, *port = NULL, *ca = NULL;
    int once = 0;
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--home") && i + 1 < argc)
            home = argv[++i];
        else if (!strcmp(argv[i], "--server") && i + 1 < argc)
            server = argv[++i];
        else if (!strcmp(argv[i], "--port") && i + 1 < argc)
            port = argv[++i];
        else if (!strcmp(argv[i], "--ca") && i + 1 < argc)
            ca = argv[++i];
        else if (!strcmp(argv[i], "--once"))
            once = 1;
        else
            return 2;
    }
    int lock = service_lock(home, "relay");
    if (lock < 0)
        return 1;
    signal(SIGTERM, stop);
    signal(SIGINT, stop);
    signal(SIGPIPE, SIG_IGN);
    int result = 1;
    while (running) {
        result = session(home, server, port, ca);
        if (once)
            break;
        for (int i = 0; i < 30 && running; i++)
            usleep(100000);
    }
    close(lock);
    return result == 0 ? 0 : 1;
}
