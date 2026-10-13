#include "native.h"
#include <stdio.h>
#include <string.h>

/* Native submission uses UTF-16 SMS: one segment, at most 70 code units. */
int sms_submit(const char *recipient, const char *body, char *out, size_t cap) {
    const char *digits = *recipient == '+' ? recipient + 1 : recipient;
    size_t length = strlen(digits);
    if (length < 3 || length > 20 || !*body)
        return -1;
    for (size_t i = 0; i < length; i++)
        if (digits[i] < '0' || digits[i] > '9')
            return -1;
    unsigned char data[180];
    size_t at = 0;
    data[at++] = 1;
    data[at++] = 0;
    data[at++] = (unsigned char)length;
    data[at++] = *recipient == '+' ? 0x91 : 0x81;
    for (size_t i = 0; i < length; i += 2)
        data[at++] =
            (unsigned char)((digits[i] - '0') | ((i + 1 < length ? digits[i + 1] - '0' : 15) << 4));
    data[at++] = 0;
    data[at++] = 8;
    size_t udl = at++;
    size_t begin = at;
    const unsigned char *s = (const unsigned char *)body;
    while (*s) {
        unsigned cp = *s++;
        int more = 0;
        unsigned minimum = 0;
        if (cp < 128) {
            if (cp < 32 && cp != '\n' && cp != '\r' && cp != '\t')
                return -1;
        } else if (cp >= 0xc2 && cp <= 0xdf) {
            more = 1;
            minimum = 128;
            cp &= 31;
        } else if (cp >= 0xe0 && cp <= 0xef) {
            more = 2;
            minimum = 2048;
            cp &= 15;
        } else if (cp >= 0xf0 && cp <= 0xf4) {
            more = 3;
            minimum = 65536;
            cp &= 7;
        } else
            return -1;
        for (int i = 0; i < more; i++) {
            if ((*s & 0xc0) != 0x80)
                return -1;
            cp = (cp << 6) | (*s++ & 63);
        }
        if (cp < minimum || cp > 0x10ffff || (cp >= 0xd800 && cp <= 0xdfff))
            return -1;
        if (at - begin + (cp > 65535 ? 4 : 2) > 140)
            return -1;
        if (cp > 65535) {
            unsigned high = 0xd800 + ((cp - 65536) >> 10), low = 0xdc00 + ((cp - 65536) & 1023);
            data[at++] = (unsigned char)(high >> 8);
            data[at++] = (unsigned char)high;
            data[at++] = (unsigned char)(low >> 8);
            data[at++] = (unsigned char)low;
        } else {
            data[at++] = (unsigned char)(cp >> 8);
            data[at++] = (unsigned char)cp;
        }
    }
    data[udl] = (unsigned char)(at - begin);
    if (cap < at * 2 + 1)
        return -1;
    for (size_t i = 0; i < at; i++)
        snprintf(out + i * 2, 3, "%02X", data[i]);
    return 0;
}
