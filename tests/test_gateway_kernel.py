"""Exercise the replacement driver function against independent session fixtures."""
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('gcc'), 'host C compiler required')
class GatewayKernelTests(unittest.TestCase):
    def test_session_selection_accounting_and_errors(self):
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#define VOICE_PLAYBACK_TX 0x8005
#define VOICE2_PLAYBACK_TX 0x8002
#define VOICE_SESSION_VSID 0x10C01000
#define VOICE2_SESSION_VSID 0x10DC1000
#define VOLTE_SESSION_VSID 0x10C02000
#define VOWLAN_SESSION_VSID 0x10002000
#define VOICEMMODE1_VSID 0x11C05000
#define VOICEMMODE2_VSID 0x11DC5000
#define MAX_VOC_SESSIONS 8
#define pr_info(...) ((void)0)
#define pr_warn(...) ((void)0)
struct voice_data {
    uint32_t session_id;
    int lock, handle, failure;
    struct { uint32_t count, play_enable; uint16_t port_id; } music_info;
};
static struct { struct voice_data voice[8]; } common;
static int starts[8], stops[8], locked;
static void mutex_lock(int *lock) { assert(!locked && !*lock); *lock = 1; locked++; }
static void mutex_unlock(int *lock) { assert(locked == 1 && *lock); *lock = 0; locked--; }
static int voice_get_cvs_handle(struct voice_data *v) { assert(locked == 1); return v->handle; }
static int voice_cvs_start_playback(struct voice_data *v) {
    assert(locked == 1); starts[v - common.voice]++; return v->failure;
}
static int voice_cvs_stop_playback(struct voice_data *v) {
    assert(locked == 1); stops[v - common.voice]++; return v->failure;
}
#include "kaiforward_incall_playback.h"
static void reset(void) {
    const uint32_t ids[8] = {VOICE_SESSION_VSID, 0x10004000, VOLTE_SESSION_VSID,
        VOICE2_SESSION_VSID, 0x10803000, VOWLAN_SESSION_VSID, VOICEMMODE1_VSID, VOICEMMODE2_VSID};
    memset(&common, 0, sizeof(common)); memset(starts, 0, sizeof(starts));
    memset(stops, 0, sizeof(stops)); locked = 0;
    for (int i = 0; i < 8; i++) common.voice[i].session_id = ids[i];
}
int main(void) {
    reset(); common.voice[6].handle = 17;
    assert(voc_start_playback(1, VOICE_PLAYBACK_TX) == 0);
    assert(starts[6] == 1 && !starts[7] && !locked);
    for (int i = 0; i < 8; i++)
        assert(common.voice[i].music_info.count == (i == 0 || i == 2 || i == 5 || i == 6));
    assert(voc_start_playback(1, VOICE_PLAYBACK_TX) == 0);
    assert(voc_start_playback(0, VOICE_PLAYBACK_TX) == 0);
    assert(common.voice[6].music_info.count == 1 && common.voice[6].music_info.play_enable);
    assert(voc_start_playback(0, VOICE_PLAYBACK_TX) == 0);
    assert(voc_start_playback(0, VOICE_PLAYBACK_TX) == 0);
    assert(!common.voice[6].music_info.count && !common.voice[6].music_info.play_enable && !locked);

    reset(); common.voice[7].handle = 18;
    assert(voc_start_playback(1, VOICE2_PLAYBACK_TX) == 0);
    assert(starts[7] == 1 && !starts[6]);
    for (int i = 0; i < 8; i++) assert(common.voice[i].music_info.count == (i == 3 || i == 7));

    reset(); common.voice[0].handle = 1; common.voice[2].handle = 2;
    assert(voc_start_playback(1, VOICE_PLAYBACK_TX) == 0);
    assert(starts[0] == 1 && starts[2] == 1);
    common.voice[0].failure = -EIO;
    assert(voc_start_playback(0, VOICE_PLAYBACK_TX) == -EIO && !locked);

    reset();
    assert(voc_start_playback(1, VOICE_PLAYBACK_TX) == -ENODEV);
    assert(common.voice[6].music_info.count == 1); /* retain handover/pending state */
    assert(voc_start_playback(0, VOICE_PLAYBACK_TX) == 0 && !locked);

    reset(); common.voice[6].handle = 1; common.voice[6].music_info.count = UINT32_MAX;
    assert(voc_start_playback(1, VOICE_PLAYBACK_TX) == -EOVERFLOW);
    assert(common.voice[6].music_info.count == UINT32_MAX && !starts[6] && !locked);

    reset();
    assert(voc_start_playback(1, 0xffff) == -EINVAL);
    assert(voc_start_playback(2, VOICE_PLAYBACK_TX) == -EINVAL);
    for (int i = 0; i < 8; i++) assert(!common.voice[i].music_info.count);
    puts("VoiceMMode1/2, legacy sessions, handover state, reference counting and errors passed");
}
'''
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source, binary = directory / 'probe.c', directory / 'probe'
            source.write_text(harness)
            subprocess.run(['gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-I', str(ROOT / 'firmware/kernel'), str(source), '-o', str(binary)],
                           check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn('VoiceMMode1/2', result.stdout)


if __name__ == '__main__':
    unittest.main()
