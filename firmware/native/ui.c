#include "font.h"
#include "native.h"
#include <fcntl.h>
#include <linux/fb.h>
#include <linux/input.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <unistd.h>

#define WIDTH 240
#define HEIGHT 320
static unsigned char pixels[WIDTH * HEIGHT * 3];
static void pixel(int x, int y, unsigned color) {
    if (x < 0 || x >= WIDTH || y < 0 || y >= HEIGHT)
        return;
    size_t i = ((size_t)y * WIDTH + (size_t)x) * 3;
    pixels[i] = (unsigned char)(color >> 16);
    pixels[i + 1] = (unsigned char)(color >> 8);
    pixels[i + 2] = (unsigned char)color;
}
static void rect(int x, int y, int w, int h, unsigned color) {
    for (int yy = y; yy < y + h; yy++)
        for (int xx = x; xx < x + w; xx++)
            pixel(xx, yy, color);
}
static void label(int x, int y, const char *s, int scale, unsigned color) {
    int start = x;
    for (; *s; s++) {
        unsigned char ch = (unsigned char)*s;
        if (ch == '\n') {
            y += 18 * scale;
            x = start;
            continue;
        }
        if (ch < 32 || ch > 126)
            ch = '?';
        for (int yy = 0; yy < 16; yy++)
            for (int xx = 0; xx < 8; xx++)
                if (font[ch - 32][yy] & (1 << (7 - xx)))
                    rect(x + xx * scale, y + yy * scale, scale, scale, color);
        x += 8 * scale;
        if (x + 8 * scale > WIDTH)
            break;
    }
}
static void file_line(const char *home, const char *name, char *out, size_t cap, int skip) {
    char path[1200];
    snprintf(path, sizeof(path), "%s/%s", home, name);
    FILE *f = fopen(path, "r");
    if (!f) {
        snprintf(out, cap, "STARTING");
        return;
    }
    for (int i = 0; i <= skip; i++)
        if (!fgets(out, (int)cap, f)) {
            snprintf(out, cap, "STARTING");
            break;
        }
    fclose(f);
    out[strcspn(out, "\r\n")] = 0;
}
static int battery(void) {
    FILE *f = fopen("/sys/class/power_supply/battery/capacity", "r");
    int n = -1;
    if (f) {
        if (fscanf(f, "%d", &n) != 1)
            n = -1;
        fclose(f);
    }
    return n;
}
static int wifi(void) {
    FILE *f = fopen("/sys/class/net/wlan0/carrier", "r");
    int n = 0;
    if (f) {
        if (fscanf(f, "%d", &n) != 1)
            n = 0;
        fclose(f);
    }
    return n;
}
static void backlight(int on) {
    const char *brightness = "/sys/class/leds/lcd-backlight/brightness";
    FILE *f = fopen("/sys/class/leds/lcd-backlight/max_brightness", "r");
    int maximum = 255;
    if (f) {
        if (fscanf(f, "%d", &maximum) != 1 || maximum < 1 || maximum > 65535)
            maximum = 255;
        fclose(f);
    }
    int fd = open(brightness, O_WRONLY);
    if (fd >= 0) {
        char value[24];
        int n = snprintf(value, sizeof(value), "%d\n", on ? (maximum + 1) / 2 : 0);
        write(fd, value, (size_t)n);
        close(fd);
    }
}
static const char *names[] = {"Wi-Fi network", "Wi-Fi password", "SMS receiver URL",
                               "Receiver token", "Call relay URL"};
static char *field(Config *c, int item) {
    switch (item) {
    case 0:
        return c->ssid;
    case 1:
        return c->password;
    case 2:
        return c->url;
    case 3:
        return c->token;
    default:
        return c->relay_url;
    }
}
static size_t capacity(int item) {
    switch (item) {
    case 0:
        return 33;
    case 1:
        return 129;
    case 2:
        return 1025;
    default:
        return 257; /* token and relay URL */
    }
}
static WifiNetwork networks[KF_NETWORKS];
static int network_count = 0, network_item = 0;
static void request_scan(const char *home) {
    char path[1200];
    snprintf(path, sizeof(path), "%s/scan", home);
    atomic_write(path, "");
}
static void render(const char *home, int page, int item, int editing, int mode, Config *c,
                   const char *value, const char *notice) {
    (void)c;
    memset(pixels, 0, sizeof(pixels));
    char line[150], status[120];
    int b = battery();
    snprintf(line, sizeof(line), b < 0 ? "Battery --" : "Battery %d%%", b);
    label(8, 46, line, 1, 0xbbcbd5);
    if (!page) {
        label(8, 74, wifi() ? "Wi-Fi connected" : "Wi-Fi disconnected", 1,
              wifi() ? 0x6de3aa : 0xffc66d);
        file_line(home, "radio.status", status, sizeof(status), 0);
        label(8, 103, status, 1, 0xffffff);
        file_line(home, "radio.status", status, sizeof(status), 1);
        label(8, 126, status, 1, 0xffffff);
        file_line(home, "radio.status", status, sizeof(status), 2);
        label(8, 164, status, 1, 0xffffff);
        file_line(home, "radio.status", status, sizeof(status), 3);
        label(8, 187, status, 1, 0xffc66d);
        file_line(home, "upload.status", status, sizeof(status), 1);
        label(8, 222, status, 1, 0xbbcbd5);
        file_line(home, "radio.status", status, sizeof(status), 5);
        label(8, 246, status, 1, strstr(status, "incoming") ? 0xffc66d : 0xbbcbd5);
        label(8, 268, "OK: Settings", 1, 0xffffff);
        label(8, 298, "Sync                 Sleep", 1, 0x6de3aa);
    } else if (page == 2) {
        label(8, 74, "Wi-Fi networks", 1, 0xbbcbd5);
        if (!network_count) {
            label(8, 113, "Scanning...", 1, 0xffc66d);
            label(8, 143, "1: Scan again", 1, 0xbbcbd5);
        }
        int start = (network_item / 6) * 6;
        for (int i = start; i < network_count && i < start + 6; i++) {
            int y = 102 + (i - start) * 27;
            if (i == network_item)
                rect(4, y - 2, 232, 25, 0x153a4b);
            char entry[64];
            snprintf(entry, sizeof(entry), "%.25s %c", networks[i].ssid,
                     networks[i].security == 1   ? '+'
                     : networks[i].security == 2 ? '!'
                                                 : ' ');
            label(8, y, entry, 1, 0xffffff);
        }
        label(8, 269, "OK: Choose  1: Scan", 1, 0xbbcbd5);
        label(8, 298, "Manual               Back", 1, 0x6de3aa);
    } else if (!editing) {
        label(8, 73, "Settings", 1, 0xbbcbd5);
        for (int i = 0; i < 5; i++) {
            if (i == item)
                rect(4, 99 + i * 31, 232, 28, 0x153a4b);
            label(8, 104 + i * 31, names[i], 1, 0xffffff);
        }
        label(8, 268, "OK: Edit", 1, 0xbbcbd5);
        label(8, 298, "                     Back", 1, 0x6de3aa);
    } else {
        label(8, 80, names[item], 1, 0xffffff);
        size_t len = strlen(value);
        size_t begin = len > 96 ? len - 96 : 0;
        char shown[100];
        snprintf(shown, sizeof(shown), "%s", value + begin);
        for (size_t at = 0; at < strlen(shown); at += 28) {
            char part[29];
            snprintf(part, sizeof(part), "%.28s", shown + at);
            label(8, 115 + (int)(at / 28) * 22, part, 1, 0x6de3aa);
        }
        label(8, 226, mode == 2 ? "123 mode" : "Multi-tap: 0-9, * symbols", 1, 0xbbcbd5);
        label(8, 246, "#: case/123   End: delete", 1, 0xbbcbd5);
        label(8, 298, "Save               Cancel", 1, 0x6de3aa);
    }
    if (*notice) {
        rect(0, 278, WIDTH, 19, 0x5d3813);
        label(8, 278, notice, 1, 0xffc66d);
    }
}
static unsigned channel(unsigned char value, struct fb_bitfield b) {
    return b.length ? ((unsigned)value >> (8 - b.length)) << b.offset : 0;
}
static int present(int fd, unsigned char *memory, const struct fb_fix_screeninfo *fix,
                   struct fb_var_screeninfo *var) {
    unsigned bytes = var->bits_per_pixel / 8;
    if ((bytes != 2 && bytes != 4) || var->xres != WIDTH || var->yres != HEIGHT ||
        var->red.length > 8 || var->green.length > 8 || var->blue.length > 8)
        return -1;
    if (fix->line_length < WIDTH * bytes ||
        var->red.offset + var->red.length > var->bits_per_pixel ||
        var->green.offset + var->green.length > var->bits_per_pixel ||
        var->blue.offset + var->blue.length > var->bits_per_pixel)
        return -1;
    if ((size_t)fix->line_length * HEIGHT > fix->smem_len)
        return -1;
    for (unsigned y = 0; y < HEIGHT; y++)
        for (unsigned x = 0; x < WIDTH; x++) {
            size_t i = ((size_t)y * WIDTH + x) * 3;
            unsigned v = channel(pixels[i], var->red) | channel(pixels[i + 1], var->green) |
                         channel(pixels[i + 2], var->blue);
            unsigned char *p = memory + (size_t)y * fix->line_length + x * bytes;
            for (unsigned k = 0; k < bytes; k++)
                p[k] = (unsigned char)(v >> (k * 8));
        }
    var->xoffset = var->yoffset = 0;
    var->activate = FB_ACTIVATE_VBL;
    return ioctl(fd, FBIOPAN_DISPLAY, var);
}
void ui_run(const char *home, const char *preview) {
    Config c;
    if (config_load(home, &c))
        memset(&c, 0, sizeof(c));
    if (preview) {
        render(home, 0, 0, 0, 0, &c, "", "");
        FILE *f = fopen(preview, "wb");
        if (!f)
            exit(1);
        fprintf(f, "P6\n%d %d\n255\n", WIDTH, HEIGHT);
        fwrite(pixels, 1, sizeof(pixels), f);
        fclose(f);
        return;
    }
    int fb = open("/dev/graphics/fb0", O_RDWR);
    struct fb_fix_screeninfo fix;
    struct fb_var_screeninfo var;
    if (fb < 0 || ioctl(fb, FBIOGET_FSCREENINFO, &fix) || ioctl(fb, FBIOGET_VSCREENINFO, &var)) {
        fprintf(stderr, "Native framebuffer unavailable\n");
        exit(1);
    }
    unsigned char *mem = mmap(NULL, fix.smem_len, PROT_READ | PROT_WRITE, MAP_SHARED, fb, 0);
    if (mem == MAP_FAILED)
        exit(1);
    struct pollfd fds[16];
    int nfds = 0;
    for (int i = 0; i < 16; i++) {
        char path[80];
        snprintf(path, sizeof(path), "/dev/input/event%d", i);
        int fd = open(path, O_RDONLY | O_NONBLOCK);
        if (fd >= 0) {
            fds[nfds].fd = fd;
            fds[nfds].events = POLLIN;
            nfds++;
        }
    }
    int page = 0, item = 0, editing = 0, mode = 0, sleeping = 0, last_key = -1, tap = 0;
    char value[1025] = "", notice[100] = "", previous_counts[120] = "";
    file_line(home, "radio.status", previous_counts, sizeof(previous_counts), 2);
    int64_t last = monotonic_ms(), last_tap = 0, notice_until = 0;
    ioctl(fb, FBIOBLANK, FB_BLANK_UNBLANK);
    backlight(1);
    for (;;) {
        int64_t now = monotonic_ms();
        char counts[120], call[120];
        file_line(home, "radio.status", counts, sizeof(counts), 2);
        file_line(home, "radio.status", call, sizeof(call), 5);
        int previous_received = 0, received = 0;
        char *old_count = strstr(previous_counts, "Received: ");
        char *new_count = strstr(counts, "Received: ");
        if (old_count)
            previous_received = atoi(old_count + 10);
        if (new_count)
            received = atoi(new_count + 10);
        strcpy(previous_counts, counts);
        if (strstr(call, "incoming") || received > previous_received) {
            if (sleeping) {
                ioctl(fb, FBIOBLANK, FB_BLANK_UNBLANK);
                backlight(1);
                sleeping = 0;
            }
            last = now;
            if (strstr(call, "incoming")) {
                snprintf(notice, sizeof(notice), "%.90s", call);
                notice_until = now + 1500;
            } else if (received > previous_received) {
                snprintf(notice, sizeof(notice), "SMS received");
                notice_until = now + 10000;
            }
        }
        if (now > notice_until)
            notice[0] = 0;
        if (!sleeping && now - last > 30000) {
            backlight(0);
            ioctl(fb, FBIOBLANK, FB_BLANK_POWERDOWN);
            sleeping = 1;
        }
        if (!sleeping) {
            if (!editing)
                config_load(home, &c);
            if (page == 2) {
                network_count = wifi_load(home, networks, KF_NETWORKS);
                if (network_item >= network_count)
                    network_item = network_count ? network_count - 1 : 0;
            }
            render(home, page, item, editing, mode, &c, value, notice);
            if (present(fb, mem, &fix, &var)) {
                fprintf(stderr, "Native display commit failed\n");
                exit(1);
            }
        }
        int ready = poll(fds, (nfds_t)nfds, 500);
        if (ready <= 0)
            continue;
        for (int i = 0; i < nfds; i++)
            if (fds[i].revents & POLLIN) {
                struct input_event ev;
                while (read(fds[i].fd, &ev, sizeof(ev)) == sizeof(ev)) {
                    if (ev.type != EV_KEY || ev.value != 1)
                        continue;
                    int key = ev.code;
                    last = monotonic_ms();
                    if (sleeping) {
                        ioctl(fb, FBIOBLANK, FB_BLANK_UNBLANK);
                        backlight(1);
                        sleeping = 0;
                        continue;
                    }
                    if (!page) {
                        if (key == 139) {
                            char path[1200];
                            snprintf(path, sizeof(path), "%s/sync", home);
                            atomic_write(path, "");
                        } else if (key == 352 || key == 28) {
                            page = 1;
                            config_load(home, &c);
                        } else if (key == 158 || key == 116) {
                            backlight(0);
                            ioctl(fb, FBIOBLANK, FB_BLANK_POWERDOWN);
                            sleeping = 1;
                        }
                        continue;
                    }
                    if (page == 2) {
                        if (key == 103 && network_count)
                            network_item = (network_item + network_count - 1) % network_count;
                        else if (key == 108 && network_count)
                            network_item = (network_item + 1) % network_count;
                        else if (key == 158 || key == 116)
                            page = 1;
                        else if (key == 2)
                            request_scan(home);
                        else if (key == 139) {
                            page = 1;
                            item = 0;
                            editing = 1;
                            strcpy(value, c.ssid);
                            last_key = -1;
                        } else if ((key == 352 || key == 28) && network_count) {
                            WifiNetwork *chosen = &networks[network_item];
                            if (chosen->security == 2) {
                                snprintf(notice, sizeof(notice), "Unsupported security");
                                notice_until = last + 4000;
                                continue;
                            }
                            Config changed = c;
                            if (strcmp(changed.ssid, chosen->ssid) || !chosen->security)
                                changed.password[0] = 0;
                            strcpy(changed.ssid, chosen->ssid);
                            if (chosen->security == 0) {
                                if (config_save(home, &changed)) {
                                    snprintf(notice, sizeof(notice), "Save failed");
                                    notice_until = last + 4000;
                                } else {
                                    c = changed;
                                    page = 1;
                                }
                            } else {
                                /* Commit SSID and password together when Save is pressed. */
                                c = changed;
                                page = 1;
                                item = 1;
                                editing = 1;
                                strcpy(value, c.password);
                                last_key = -1;
                            }
                        }
                        continue;
                    }
                    if (!editing) {
                        if (key == 103)
                            item = (item + 4) % 5;
                        else if (key == 108)
                            item = (item + 1) % 5;
                        else if (key == 158 || key == 116)
                            page = 0;
                        else if (key == 352 || key == 28) {
                            if (item == 0) {
                                page = 2;
                                network_item = 0;
                                request_scan(home);
                                continue;
                            }
                            editing = 1;
                            strcpy(value, field(&c, item));
                            last_key = -1;
                        }
                        continue;
                    }
                    if (key == 158) {
                        config_load(home, &c);
                        editing = 0;
                        continue;
                    }
                    if (key == 139 || key == 352 || key == 28) {
                        Config changed = c;
                        strcpy(field(&changed, item), value);
                        if (config_save(home, &changed)) {
                            snprintf(notice, sizeof(notice), "Invalid / save failed");
                            notice_until = last + 4000;
                        } else {
                            c = changed;
                            editing = 0;
                            char path[1200];
                            snprintf(path, sizeof(path), "%s/reload", home);
                            atomic_write(path, "");
                            snprintf(path, sizeof(path), "%s/sync", home);
                            atomic_write(path, "");
                        }
                        continue;
                    }
                    size_t n = strlen(value);
                    if (key == 116) {
                        if (n)
                            value[n - 1] = 0;
                        last_key = -1;
                        continue;
                    }
                    if (key == 523) {
                        mode = (mode + 1) % 3;
                        last_key = -1;
                        continue;
                    }
                    int digit = key >= 2 && key <= 10 ? key - 1 : key == 11 ? 0 : -1;
                    const char *letters = NULL;
                    const char *map[] = {" 0",   ".,?!1", "abc2",  "def3", "ghi4",
                                         "jkl5", "mno6",  "pqrs7", "tuv8", "wxyz9"};
                    if (key == 522)
                        letters = "/:@-_?=&%+.*";
                    else if (digit >= 0)
                        letters = map[digit];
                    if (!letters)
                        continue;
                    if (mode == 2 && digit >= 0) {
                        char num[2] = {(char)('0' + digit), 0};
                        if (n + 1 < capacity(item)) {
                            value[n] = num[0];
                            value[n + 1] = 0;
                        }
                        last_key = -1;
                        continue;
                    }
                    if (key == last_key && last - last_tap < 900 && n) {
                        tap = (tap + 1) % (int)strlen(letters);
                        n--;
                    } else
                        tap = 0;
                    if (n + 1 < capacity(item)) {
                        char ch = letters[tap];
                        if (mode == 1 && ch >= 'a' && ch <= 'z')
                            ch = (char)(ch - 32);
                        value[n] = ch;
                        value[n + 1] = 0;
                    }
                    last_key = key;
                    last_tap = last;
                }
            }
    }
}
