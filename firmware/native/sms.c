#include "native.h"
#include <ctype.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

/* 3GPP TS 23.038 default alphabet; unsupported national tables are retained raw. */
static const uint16_t gsm[128] = {
    64,  163, 36,  165, 232, 233, 249, 236, 242, 199, 10,  216, 248, 13,  197, 229, 916, 95,  934,
    915, 923, 937, 928, 936, 931, 920, 926, 0,   198, 230, 223, 201, 32,  33,  34,  35,  164, 37,
    38,  39,  40,  41,  42,  43,  44,  45,  46,  47,  48,  49,  50,  51,  52,  53,  54,  55,  56,
    57,  58,  59,  60,  61,  62,  63,  161, 65,  66,  67,  68,  69,  70,  71,  72,  73,  74,  75,
    76,  77,  78,  79,  80,  81,  82,  83,  84,  85,  86,  87,  88,  89,  90,  196, 214, 209, 220,
    167, 191, 97,  98,  99,  100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113,
    114, 115, 116, 117, 118, 119, 120, 121, 122, 228, 246, 241, 252, 224};
static int utf8(char *out, size_t cap, size_t *n, unsigned cp) {
    if (cp == 0 || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
        return -1;
    if (*n + 5 >= cap)
        return -1;
    if (cp < 128)
        out[(*n)++] = (char)cp;
    else if (cp < 2048) {
        out[(*n)++] = (char)(0xc0 | (cp >> 6));
        out[(*n)++] = (char)(0x80 | (cp & 63));
    } else if (cp < 65536) {
        out[(*n)++] = (char)(0xe0 | (cp >> 12));
        out[(*n)++] = (char)(0x80 | ((cp >> 6) & 63));
        out[(*n)++] = (char)(0x80 | (cp & 63));
    } else {
        out[(*n)++] = (char)(0xf0 | (cp >> 18));
        out[(*n)++] = (char)(0x80 | ((cp >> 12) & 63));
        out[(*n)++] = (char)(0x80 | ((cp >> 6) & 63));
        out[(*n)++] = (char)(0x80 | (cp & 63));
    }
    out[*n] = 0;
    return 0;
}
static unsigned extension(unsigned c) {
    switch (c) {
    case 10:
        return 12;
    case 20:
        return '^';
    case 40:
        return '{';
    case 41:
        return '}';
    case 47:
        return '\\';
    case 60:
        return '[';
    case 61:
        return '~';
    case 62:
        return ']';
    case 64:
        return '|';
    case 101:
        return 8364;
    default:
        return 0;
    }
}
static int septets(const unsigned char *p, size_t bytes, int start, int count, char *out,
                   size_t cap) {
    size_t n = 0;
    int escape = 0;
    out[0] = 0;
    for (int i = 0; i < count; i++) {
        int bit = (start + i) * 7;
        if (bit + 7 > (int)bytes * 8)
            return -1;
        unsigned v = p[bit / 8] >> (bit % 8);
        if (bit % 8 > 1)
            v |= (unsigned)p[bit / 8 + 1] << (8 - bit % 8);
        v &= 127;
        if (!escape && v == 27) {
            escape = 1;
            continue;
        }
        unsigned cp = escape ? extension(v) : gsm[v];
        escape = 0;
        if (utf8(out, cap, &n, cp))
            return -1;
    }
    return escape ? -1 : 0;
}
static int nibble(char c) {
    if (c >= '0' && c <= '9')
        return c - '0';
    c = (char)tolower((unsigned char)c);
    if (c >= 'a' && c <= 'f')
        return c - 'a' + 10;
    return -1;
}
static int bcd(unsigned v) {
    if ((v & 15) > 9 || (v >> 4) > 9)
        return -1;
    return (int)(v & 15) * 10 + (int)(v >> 4);
}
int sms_decode(const char *hex, Sms *s) {
    unsigned char p[512];
    size_t n = strlen(hex), at = 0;
    memset(s, 0, sizeof(*s));
    s->total = s->part = 1;
    s->reference = -1;
    if (n < 4 || n % 2 || n / 2 > sizeof(p))
        return -1;
    for (size_t i = 0; i < n / 2; i++) {
        int a = nibble(hex[2 * i]), b = nibble(hex[2 * i + 1]);
        if (a < 0 || b < 0)
            return -1;
        p[i] = (unsigned char)(a * 16 + b);
    }
    n /= 2;
    at = 1 + p[0];
    if (at + 3 > n)
        return -1;
    unsigned flags = p[at++];
    if ((flags & 3) != 0)
        return -1; /* SMS-DELIVER */
    int digits = p[at++];
    unsigned toa = p[at++];
    size_t address = (size_t)(digits + 1) / 2;
    if (at + address + 10 > n)
        return -1;
    if ((toa & 0x70) == 0x50) {
        if (septets(p + at, address, 0, digits * 4 / 7, s->sender, sizeof(s->sender)))
            return -1;
    } else {
        size_t pos = 0;
        if ((toa & 0x70) == 0x10)
            s->sender[pos++] = '+';
        if ((size_t)digits + pos >= sizeof(s->sender))
            return -1;
        const char *alphabet = "0123456789*#abc";
        for (int i = 0; i < digits; i++) {
            unsigned d = (p[at + (size_t)i / 2] >> (i % 2 * 4)) & 15;
            if (d == 15)
                return -1;
            s->sender[pos++] = alphabet[d];
        }
        s->sender[pos] = 0;
    }
    at += address;
    at++;
    unsigned dcs = p[at++];
    if ((dcs & 0xc0) == 0) {
        if (dcs & 0x20)
            return -1;
        s->encoding = (int)((dcs >> 2) & 3);
    } else if ((dcs & 0xf0) == 0xf0)
        s->encoding = (dcs & 4) ? 1 : 0;
    else if ((dcs & 0xf0) == 0xc0 || (dcs & 0xf0) == 0xd0)
        s->encoding = 0;
    else if ((dcs & 0xf0) == 0xe0)
        s->encoding = 2;
    else
        return -1;
    if (s->encoding != 0 && s->encoding != 2)
        return -1;
    int v[7];
    for (int i = 0; i < 7; i++) {
        unsigned b = p[at++];
        v[i] = bcd(i == 6 ? b & 0xf7 : b);
        if (v[i] < 0)
            return -1;
    }
    if (v[1] < 1 || v[1] > 12 || v[2] < 1 || v[2] > 31 || v[3] > 23 || v[4] > 59 || v[5] > 59 ||
        v[6] > 56)
        return -1;
    struct tm tm = {0};
    tm.tm_year = (v[0] >= 70 ? v[0] : v[0] + 100);
    tm.tm_mon = v[1] - 1;
    tm.tm_mday = v[2];
    tm.tm_hour = v[3];
    tm.tm_min = v[4];
    tm.tm_sec = v[5];
    int sign = (p[at - 1] & 8) ? -1 : 1;
    s->timestamp = ((int64_t)timegm(&tm) - sign * v[6] * 15 * 60) * 1000;
    if (s->timestamp < 0)
        return -1;
    int length = p[at++];
    size_t bytes = s->encoding == 0 ? (size_t)(length * 7 + 7) / 8 : (size_t)length;
    if (bytes > 140 || at + bytes > n)
        return -1;
    const unsigned char *ud = p + at;
    size_t header = 0;
    if (flags & 64) {
        if (!bytes)
            return -1;
        header = (size_t)ud[0] + 1;
        if (header > bytes)
            return -1;
        for (size_t i = 1; i < header;) {
            if (i + 2 > header)
                return -1;
            int type = ud[i++], size = ud[i++];
            if (i + (size_t)size > header)
                return -1;
            if (type == 0 && size == 3) {
                s->reference = ud[i];
                s->total = ud[i + 1];
                s->part = ud[i + 2];
            } else if (type == 8 && size == 4) {
                s->reference = ud[i] * 256 + ud[i + 1];
                s->total = ud[i + 2];
                s->part = ud[i + 3];
            } else
                return -1; /* Do not silently reinterpret port-addressed/national-table SMS. */
            i += (size_t)size;
        }
        if (s->total < 1 || s->part < 1 || s->part > s->total)
            return -1;
    }
    if (!s->encoding) {
        int skipped = (int)(header * 8 + 6) / 7;
        if (skipped > length)
            return -1;
        return septets(ud, bytes, skipped, length - skipped, s->body, sizeof(s->body));
    }
    if ((bytes - header) % 2)
        return -1;
    size_t pos = 0;
    for (size_t i = header; i < bytes; i += 2) {
        unsigned cp = ud[i] * 256 + ud[i + 1];
        if (cp >= 0xd800 && cp <= 0xdbff) {
            if (i + 3 >= bytes)
                return -1;
            unsigned low = ud[i + 2] * 256 + ud[i + 3];
            if (low < 0xdc00 || low > 0xdfff)
                return -1;
            cp = 0x10000 + (cp - 0xd800) * 1024 + low - 0xdc00;
            i += 2;
        }
        if (utf8(s->body, sizeof(s->body), &pos, cp))
            return -1;
    }
    return 0;
}
