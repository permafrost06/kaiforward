#ifndef KF_CALL_PCM_H
#define KF_CALL_PCM_H
#include <sys/types.h>
typedef struct {
    pid_t capture_pid, playback_pid;
    int capture, playback;
    int session, routes;
} CallAudio;
int audio_start(CallAudio *audio, int network_fd);
void audio_stop(CallAudio *audio);
int audio_alive(CallAudio *audio);
int audio_mode(int in_call);
#endif
