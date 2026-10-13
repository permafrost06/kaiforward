/* Direct Linux ALSA PCM access; no KaiOS app or Bionic ABI dependency. */
#define _GNU_SOURCE
#include "pcm.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <signal.h>
#include <sound/asound.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <unistd.h>

static int utility(const char *program, const char *a, const char *b, const char *c, const char *d,
                   const char *e, char *output, size_t cap) {
    int pipes[2];
    if (pipe2(pipes, O_CLOEXEC | O_NONBLOCK))
        return -1;
    pid_t pid = fork();
    if (pid < 0) {
        close(pipes[0]);
        close(pipes[1]);
        return -1;
    }
    if (!pid) {
        prctl(PR_SET_PDEATHSIG, SIGKILL);
        int flags = fcntl(pipes[1], F_GETFL, 0);
        if (flags >= 0)
            fcntl(pipes[1], F_SETFL, flags & ~O_NONBLOCK);
        dup2(pipes[1], STDOUT_FILENO);
        dup2(pipes[1], STDERR_FILENO);
        close(pipes[0]);
        close(pipes[1]);
        execl(program, program, a, b, c, d, e, (char *)NULL);
        _exit(127);
    }
    close(pipes[1]);
    int status = 0, done = 0;
    size_t used = 0;
    for (int i = 0; i < 300; i++) {
        char discard[1024];
        ssize_t n = read(pipes[0], output && used < cap - 1 ? output + used : discard,
                         output && used < cap - 1 ? cap - 1 - used : sizeof(discard));
        if (n > 0 && output && used < cap - 1)
            used += n;
        if (waitpid(pid, &status, WNOHANG) == pid) {
            done = 1;
            break;
        }
        usleep(10000);
    }
    if (output) {
        ssize_t n = read(pipes[0], output + used, cap - 1 - used);
        if (n > 0)
            used += n;
        output[used] = 0;
    }
    close(pipes[0]);
    if (!done) {
        kill(pid, SIGKILL);
        waitpid(pid, NULL, WNOHANG);
        return -1;
    }
    return WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
}
int audio_mode(int in_call) {
    char output[512];
    if (utility("/system/bin/service", "call", "media.audio_policy", "3", "i32",
                in_call ? "2" : "0", output, sizeof(output)))
        return -1;
    /* Android 6 IAudioPolicyService::SET_PHONE_STATE returns status_t. */
    return strstr(output, "Parcel(00000000") ? 0 : -1;
}
static const char *injection(int session) {
    return session == 2 ? "Incall_Music_2 Audio Mixer MultiMedia2"
                        : "Incall_Music Audio Mixer MultiMedia2";
}
static int mixer(const char *name, int value) {
    return utility("/system/bin/tinymix", name, value ? "1" : "0", NULL, NULL, NULL, NULL, 0);
}
static int state_file(int device, char stream, const char *wanted) {
    char path[128], contents[2048];
    snprintf(path, sizeof(path), "/proc/asound/card0/pcm%d%c/sub0/status", device, stream);
    FILE *f = fopen(path, "r");
    if (!f)
        return 0;
    size_t n = fread(contents, 1, sizeof(contents) - 1, f);
    fclose(f);
    contents[n] = 0;
    return strstr(contents, wanted) != NULL;
}
static void exact(struct snd_pcm_hw_params *p, int key, unsigned value) {
    if (key <= SNDRV_PCM_HW_PARAM_LAST_MASK) {
        struct snd_mask *mask = &p->masks[key];
        memset(mask, 0, sizeof(*mask));
        mask->bits[value / 32] = 1u << (value % 32);
    } else {
        struct snd_interval *interval = &p->intervals[key - SNDRV_PCM_HW_PARAM_FIRST_INTERVAL];
        interval->min = interval->max = value;
        interval->integer = 1;
    }
}
static int open_pcm(int capture) {
    int fd = open(capture ? "/dev/snd/pcmC0D0c" : "/dev/snd/pcmC0D1p", O_RDWR | O_CLOEXEC);
    if (fd < 0)
        return -1;
    struct snd_pcm_hw_params p = {0};
    for (size_t i = 0; i < sizeof(p.masks) / sizeof(p.masks[0]); i++)
        memset(&p.masks[i], 0xff, sizeof(p.masks[i]));
    for (size_t i = 0; i < sizeof(p.intervals) / sizeof(p.intervals[0]); i++)
        p.intervals[i].max = UINT_MAX;
    unsigned period = capture ? 160 : 240, periods = capture ? 4 : 2;
    exact(&p, SNDRV_PCM_HW_PARAM_ACCESS, SNDRV_PCM_ACCESS_RW_INTERLEAVED);
    exact(&p, SNDRV_PCM_HW_PARAM_FORMAT, SNDRV_PCM_FORMAT_S16_LE);
    exact(&p, SNDRV_PCM_HW_PARAM_SUBFORMAT, SNDRV_PCM_SUBFORMAT_STD);
    exact(&p, SNDRV_PCM_HW_PARAM_SAMPLE_BITS, 16);
    exact(&p, SNDRV_PCM_HW_PARAM_FRAME_BITS, 16);
    exact(&p, SNDRV_PCM_HW_PARAM_CHANNELS, 1);
    exact(&p, SNDRV_PCM_HW_PARAM_RATE, capture ? 8000 : 48000);
    exact(&p, SNDRV_PCM_HW_PARAM_PERIOD_SIZE, period);
    exact(&p, SNDRV_PCM_HW_PARAM_PERIODS, periods);
    p.rmask = ~0U;
    struct snd_pcm_sw_params sw = {0};
    sw.period_step = 1;
    sw.avail_min = period;
    sw.xfer_align = 1;
    sw.start_threshold = capture ? 1 : period;
    sw.stop_threshold = period * periods;
    sw.boundary = period * periods;
    while (sw.boundary * 2 <= (unsigned long)LONG_MAX - period * periods)
        sw.boundary *= 2;
    if (ioctl(fd, SNDRV_PCM_IOCTL_HW_PARAMS, &p) || ioctl(fd, SNDRV_PCM_IOCTL_SW_PARAMS, &sw) ||
        ioctl(fd, SNDRV_PCM_IOCTL_PREPARE)) {
        close(fd);
        return -1;
    }
    return fd;
}
static int transfer(int fd, int capture, int16_t *buffer, unsigned frames) {
    unsigned done = 0, retries = 0;
    while (done < frames) {
        struct snd_xferi x = {.buf = buffer + done, .frames = frames - done};
        int rc =
            ioctl(fd, capture ? SNDRV_PCM_IOCTL_READI_FRAMES : SNDRV_PCM_IOCTL_WRITEI_FRAMES, &x);
        if (rc < 0) {
            if (errno == EINTR)
                continue;
            if (errno == EPIPE && retries++ < 2 && !ioctl(fd, SNDRV_PCM_IOCTL_PREPARE))
                continue;
            return -1;
        }
        if (x.result <= 0 || (unsigned long)x.result > frames - done)
            return -1;
        done += (unsigned)x.result;
    }
    return 0;
}
static void engine(int capture, int channel, int ready, int network_fd) {
    signal(SIGTERM, SIG_DFL);
    signal(SIGINT, SIG_DFL);
    prctl(PR_SET_PDEATHSIG, SIGKILL);
    close(network_fd);
    int fd = open_pcm(capture);
    char status = fd < 0 ? 0 : 1;
    write(ready, &status, 1);
    close(ready);
    if (fd < 0)
        _exit(1);
    int16_t input[160], output[960];
    int16_t last = 0;
    for (;;) {
        if (capture) {
            if (transfer(fd, 1, input, 160))
                break;
            if (write(channel, input, sizeof(input)) < 0 && errno != EAGAIN && errno != EINTR)
                break;
        } else {
            memset(input, 0, sizeof(input));
            int16_t newest[160];
            ssize_t n;
            while ((n = read(channel, newest, sizeof(newest))) == sizeof(newest))
                memcpy(input, newest, sizeof(input));
            if (n == 0 || (n > 0 && n != sizeof(newest)))
                break;
            for (int i = 0; i < 160; i++) {
                for (int j = 0; j < 6; j++)
                    output[i * 6 + j] = last + (input[i] - last) * (j + 1) / 6;
                last = input[i];
            }
            if (transfer(fd, 0, output, 960))
                break;
        }
    }
    close(fd);
    close(channel);
    _exit(1);
}
static void reap(pid_t *pid) {
    if (*pid <= 0)
        return;
    kill(*pid, SIGTERM);
    for (int i = 0; i < 100; i++) {
        if (waitpid(*pid, NULL, WNOHANG) == *pid) {
            *pid = 0;
            return;
        }
        usleep(10000);
    }
    kill(*pid, SIGKILL);
    waitpid(*pid, NULL, WNOHANG);
    *pid = 0;
}
void audio_stop(CallAudio *a) {
    reap(&a->capture_pid);
    reap(&a->playback_pid);
    if (a->capture >= 0)
        close(a->capture);
    if (a->playback >= 0)
        close(a->playback);
    if (a->routes) {
        mixer(injection(a->session), 0);
        mixer("MultiMedia1 Mixer VOC_REC_DL", 0);
    }
    memset(a, 0, sizeof(*a));
    a->capture = a->playback = -1;
}
int audio_alive(CallAudio *a) {
    if (a->capture_pid <= 0 || a->playback_pid <= 0)
        return 0;
    if (waitpid(a->capture_pid, NULL, WNOHANG) == a->capture_pid) {
        a->capture_pid = 0;
        return 0;
    }
    if (waitpid(a->playback_pid, NULL, WNOHANG) == a->playback_pid) {
        a->playback_pid = 0;
        return 0;
    }
    return kill(a->capture_pid, 0) == 0 && kill(a->playback_pid, 0) == 0;
}
int audio_start(CallAudio *a, int network_fd) {
    memset(a, 0, sizeof(*a));
    a->capture = a->playback = -1;
    int active1 = state_file(27, 'p', "state: RUNNING") || state_file(2, 'p', "state: RUNNING") ||
                  state_file(15, 'p', "state: RUNNING") || state_file(16, 'p', "state: RUNNING");
    int active2 = state_file(28, 'p', "state: RUNNING") || state_file(13, 'p', "state: RUNNING");
    if (active1 + active2 != 1 || !state_file(0, 'c', "closed") || !state_file(1, 'p', "closed"))
        return -1;
    a->session = active2 ? 2 : 1;
    char listing[65536];
    if (utility("/system/bin/tinymix", NULL, NULL, NULL, NULL, NULL, listing, sizeof(listing)))
        return -1;
    for (char *line = strtok(listing, "\n"); line; line = strtok(NULL, "\n"))
        if ((strstr(line, "MultiMedia1 Mixer ") || strstr(line, "Audio Mixer MultiMedia2 ")) &&
            strstr(line, " On"))
            return -1;
    a->routes = 1;
    if (mixer("MultiMedia1 Mixer VOC_REC_DL", 1) || mixer(injection(a->session), 1))
        goto fail;
    int cap[2], play[2], ready[2];
    if (pipe2(cap, O_CLOEXEC | O_NONBLOCK))
        goto fail;
    if (pipe2(play, O_CLOEXEC | O_NONBLOCK)) {
        close(cap[0]);
        close(cap[1]);
        goto fail;
    }
    if (pipe2(ready, O_CLOEXEC | O_NONBLOCK)) {
        close(cap[0]);
        close(cap[1]);
        close(play[0]);
        close(play[1]);
        goto fail;
    }
    fcntl(cap[0], F_SETPIPE_SZ, 4096);
    fcntl(play[0], F_SETPIPE_SZ, 4096);
    a->capture = cap[0];
    a->playback = play[1];
    a->capture_pid = fork();
    if (!a->capture_pid) {
        close(cap[0]);
        close(play[0]);
        close(play[1]);
        close(ready[0]);
        engine(1, cap[1], ready[1], network_fd);
    }
    a->playback_pid = fork();
    if (!a->playback_pid) {
        close(play[1]);
        close(cap[0]);
        close(cap[1]);
        close(ready[0]);
        engine(0, play[0], ready[1], network_fd);
    }
    close(cap[1]);
    close(play[0]);
    close(ready[1]);
    char status[2];
    int received = 0, good = 0;
    for (int i = 0; i < 200 && received < 2; i++) {
        ssize_t n = read(ready[0], status, sizeof(status));
        if (n > 0) {
            received += n;
            for (ssize_t j = 0; j < n; j++)
                good += status[j] == 1;
        }
        usleep(10000);
    }
    close(ready[0]);
    if (received == 2 && good == 2 && a->capture_pid > 0 && a->playback_pid > 0)
        return 0;
fail:
    audio_stop(a);
    return -1;
}
