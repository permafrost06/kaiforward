/* Minimal Android 6 Binder permission controller for native mode.
 * Protocol references: AOSP IPermissionController.cpp and servicemanager.
 * Only the radio/system stack may change audio settings; other grants fail.
 */
#define BINDER_IPC_32BIT
#include "native.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/android/binder.h>
#include <stdio.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#define PERMISSION_INTERFACE "android.os.IPermissionController"
#define MAP_BYTES (1024 * 1024)
static int permission_node;
int permission_allowed(const char *name, int requester, int uid) {
    if (requester != 0 && requester != 1000 && requester != 1013)
        return 0;
    if (uid != 0 && uid != 1000 && uid != 1001 && uid != 1013)
        return 0;
    return !strcmp(name, "android.permission.MODIFY_AUDIO_SETTINGS");
}
static void put32(unsigned char *p, uint32_t value) {
    for (int i = 0; i < 4; i++)
        p[i] = (unsigned char)(value >> (i * 8));
}
static size_t string16(unsigned char *p, const char *value) {
    size_t n = strlen(value), bytes = 4 + (((n + 1) * 2 + 3) & ~(size_t)3);
    memset(p, 0, bytes);
    put32(p, (uint32_t)n);
    for (size_t i = 0; i < n; i++)
        p[4 + i * 2] = (unsigned char)value[i];
    return bytes;
}
static int write_commands(int fd, const void *data, size_t length) {
    struct binder_write_read io = {0};
    io.write_size = length;
    io.write_buffer = (binder_uintptr_t)(uintptr_t)data;
    while (io.write_consumed < io.write_size) {
        if (ioctl(fd, BINDER_WRITE_READ, &io) < 0 && errno != EINTR)
            return -1;
    }
    return 0;
}
static int command(int fd, uint32_t code, const void *data, size_t bytes) {
    unsigned char buffer[128];
    if (bytes > sizeof(buffer) - 4)
        return -1;
    put32(buffer, code);
    if (bytes)
        memcpy(buffer + 4, data, bytes);
    return write_commands(fd, buffer, 4 + bytes);
}
static int release_buffer(int fd, binder_uintptr_t buffer) {
    return command(fd, BC_FREE_BUFFER, &buffer, sizeof(buffer));
}
/* Caller sees Java's exception prefix, as required by the native proxy. */
int permission_reply(unsigned code, int requester, const unsigned char *data, size_t length,
                     unsigned char *reply, size_t cap) {
    if (cap < 96)
        return -1;
    if (code == 0x5f4e5446)
        return (int)string16(reply, PERMISSION_INTERFACE);
    if (code == 0x5f504e47)
        return 0;
    if (code < 1 || code > 3)
        return -1;
    Parcel p = {data, length, 0, 0};
    parcel_int(&p); /* StrictMode policy header. */
    char token[128], name[256];
    if (parcel_string(&p, token, sizeof(token)) || strcmp(token, PERMISSION_INTERFACE))
        return -1;
    int allowed = 0;
    if (code == 1) {
        if (parcel_string(&p, name, sizeof(name)))
            return -1;
        int pid = parcel_int(&p), uid = parcel_int(&p);
        if (p.error || p.offset != p.size || pid <= 0)
            return -1;
        allowed = permission_allowed(name, requester, uid);
    } else if (code == 2) {
        parcel_int(&p); /* No app packages are exposed by this native service. */
        if (p.error || p.offset != p.size)
            return -1;
    } else {
        if (parcel_string(&p, name, sizeof(name)) || p.offset != p.size)
            return -1;
    }
    put32(reply, 0);
    put32(reply + 4, allowed);
    return 8;
}
static int respond(int fd, const struct binder_transaction_data *transaction,
                   const unsigned char *reply, int length) {
    struct binder_transaction_data response = {0};
    int32_t error = -38;
    if (length < 0) {
        response.flags = TF_STATUS_CODE;
        response.data_size = sizeof(error);
        response.data.ptr.buffer = (binder_uintptr_t)(uintptr_t)&error;
    } else {
        response.data_size = (binder_size_t)length;
        response.data.ptr.buffer = (binder_uintptr_t)(uintptr_t)reply;
    }
    if (!(transaction->flags & TF_ONE_WAY) && command(fd, BC_REPLY, &response, sizeof(response)))
        return -1;
    return release_buffer(fd, transaction->data.ptr.buffer);
}
static int process(int fd, const unsigned char *buffer, size_t length, void *mapping,
                   int *registered) {
    size_t at = 0;
    while (at + 4 <= length) {
        uint32_t code;
        memcpy(&code, buffer + at, 4);
        at += 4;
        size_t size = _IOC_SIZE(code);
        if (size > length - at)
            return -1;
        if (code == BR_INCREFS || code == BR_ACQUIRE) {
            if (command(fd, code == BR_INCREFS ? BC_INCREFS_DONE : BC_ACQUIRE_DONE, buffer + at,
                        sizeof(struct binder_ptr_cookie)))
                return -1;
        } else if (code == BR_REPLY || code == BR_TRANSACTION) {
            struct binder_transaction_data t;
            memcpy(&t, buffer + at, sizeof(t));
            uintptr_t start = (uintptr_t)mapping, ptr = (uintptr_t)t.data.ptr.buffer;
            int valid = t.data_size <= 4096 &&
                        (!t.data_size || (ptr >= start && ptr - start <= MAP_BYTES - t.data_size));
            if (code == BR_REPLY) {
                int32_t status = -1;
                if (valid && t.data_size == 4)
                    memcpy(&status, (void *)ptr, 4);
                if (release_buffer(fd, t.data.ptr.buffer) || status != 0)
                    return -1;
                *registered = 1;
                puts("Native audio permission controller registered.");
                fflush(stdout);
            } else {
                unsigned char reply[128];
                int n = -1;
                if (valid && !t.offsets_size &&
                    t.target.ptr == (binder_uintptr_t)(uintptr_t)&permission_node)
                    n = permission_reply(t.code, t.sender_euid, (void *)ptr, t.data_size, reply,
                                         sizeof(reply));
                if (respond(fd, &t, reply, n))
                    return -1;
            }
        } else if (code == BR_ERROR || code == BR_DEAD_REPLY || code == BR_FAILED_REPLY)
            return -1;
        else if (code != BR_NOOP && code != BR_TRANSACTION_COMPLETE && code != BR_RELEASE &&
                 code != BR_DECREFS && code != BR_SPAWN_LOOPER && code != BR_OK)
            return -1;
        at += size;
    }
    return at == length ? 0 : -1;
}
int permission_run(void) {
    int fd = open("/dev/binder", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return 1;
    struct binder_version version;
    if (ioctl(fd, BINDER_VERSION, &version) || version.protocol_version != 7) {
        fprintf(stderr, "Native permission controller requires Binder protocol 7\n");
        close(fd);
        return 1;
    }
    void *mapping = mmap(NULL, MAP_BYTES, PROT_READ, MAP_PRIVATE, fd, 0);
    if (mapping == MAP_FAILED) {
        close(fd);
        return 1;
    }
    unsigned threads = 0;
    if (ioctl(fd, BINDER_SET_MAX_THREADS, &threads))
        goto fail;
    unsigned char parcel[256] = {0};
    size_t used = 4;
    used += string16(parcel + used, "android.os.IServiceManager");
    used += string16(parcel + used, "permission");
    binder_size_t offset = (binder_size_t)used;
    struct flat_binder_object object = {0};
    object.hdr.type = BINDER_TYPE_BINDER;
    object.flags = 0x7f | FLAT_BINDER_FLAG_ACCEPTS_FDS;
    object.binder = object.cookie = (binder_uintptr_t)(uintptr_t)&permission_node;
    memcpy(parcel + used, &object, sizeof(object));
    used += sizeof(object);
    put32(parcel + used, 0);
    used += 4; /* allowIsolated = false. */
    struct binder_transaction_data transaction = {0};
    transaction.code = 3; /* IServiceManager.addService */
    transaction.flags = TF_ACCEPT_FDS;
    transaction.data_size = used;
    transaction.offsets_size = sizeof(offset);
    transaction.data.ptr.buffer = (binder_uintptr_t)(uintptr_t)parcel;
    transaction.data.ptr.offsets = (binder_uintptr_t)(uintptr_t)&offset;
    if (command(fd, BC_TRANSACTION, &transaction, sizeof(transaction)) ||
        command(fd, BC_ENTER_LOOPER, NULL, 0))
        goto fail;
    int registered = 0;
    for (;;) {
        unsigned char buffer[4096];
        struct binder_write_read io = {0};
        io.read_size = sizeof(buffer);
        io.read_buffer = (binder_uintptr_t)(uintptr_t)buffer;
        if (ioctl(fd, BINDER_WRITE_READ, &io) < 0) {
            if (errno == EINTR)
                continue;
            break;
        }
        if (process(fd, buffer, io.read_consumed, mapping, &registered))
            break;
    }
fail:
    munmap(mapping, MAP_BYTES);
    close(fd);
    return 1;
}
