# Nokia 8110 software call audio findings

Recorded: 2026-10-12; updated 2026-10-13.

**Current result:** the corrected kernel boots with the working native
ramdisk. The owner confirmed simultaneous browser-to-caller and
caller-to-browser audio over Wi-Fi, plus browser hangup, on SIM 2.
VoiceMMode1 playback was observed in the kernel log. Both streams and routes
released after hangup. See [CALL_RELAY.md](firmware/CALL_RELAY.md) for the
installed implementation, recovery packages and scope of verification.
The earlier diagnosis and failed trial history below are retained.

## Verified behavior

- Robi cellular downlink audio can be captured digitally on stock firmware. A 12-second, mono 8 kHz PCM recording contains the remote caller counting, as confirmed by the owner.
- The synthesized injection file sounds correct on the PC.
- Native MultiMedia2 playback into `Incall_Music Audio Mixer MultiMedia2` completes without an error, but the caller hears nothing.
- Explicitly applying live playback gain of 4096/8192 did not make the phrase audible. That test completed without rebooting and left VoiceMMode1 RUNNING; the injection route was restored and its PCM closed.
- The first injection attempt restarted the phone. Its cause remains unknown; the subsequent attempts did not reproduce that restart.

## Exact-firmware session mismatch

The live boot partition SHA-256 matches the previously verified stock backup. The decompressed kernel also contains the exact live version string: `3.10.49-ged30a3a-00740-gcad0df0`, built 2018-07-22. Live kernel symbols were used to locate the following functions in that image. Kernel pointer visibility was temporarily enabled for the symbol read and restored to its previous value, 2.

The Robi call snapshots show `VoiceMMode1_Tx Mixer TERT_MI2S_TX_MMode1` and `PRI_MI2S_RX_Voice Mixer VoiceMMode1` active. VoiceMMode1's PCM remained RUNNING after the diagnostic playback.

Disassembly of the stock `voc_start_playback` at `0xc06ecef4` shows:

- Playback port `0x8005` selects the session named `Voice session`.
- Playback port `0x8002` selects `Voice2 session`.
- The first path additionally considers `VoLTE session` after the legacy Voice session.
- Neither path selects `VoiceMMode1` or `VoiceMMode2`.
- The selected session's CVS handle is checked; if it is zero, the modem playback-start command is skipped. Returning success can therefore occur without starting playback on the active call.

The stock `voc_get_session_id` at `0xc06e6e14` does recognize `VoiceMMode1` and `VoiceMMode2` as distinct session entries. Their string addresses are `0xc0be9b77` and `0xc0be9b83`, respectively. This makes the omission specific to playback selection, rather than evidence that the kernel lacks these sessions altogether.

This is a concrete driver mismatch and the leading explanation for silent injection. We have not read live CVS handles or tested a corrected implementation, so it is not yet proof that fixing this omission is sufficient. It also does not explain the first attempt's restart.

## Proposed correction and remaining proof

The next implementation step is native driver/session support, rather than another sample-file or volume variation. Preserve existing Voice, Voice2 and VoLTE behavior while including the matching VoiceMMode sessions when selecting the playback backend. Select only relevant sessions, preserve start/stop accounting and locking, and surface a missing active session or modem-command failure instead of treating a silent no-op as successful injection.

Related Qualcomm implementations iterate the voice sessions and include VoiceMMode1/VoiceMMode2 in subscription-specific selection. They are useful references, not drop-in patches for this exact kernel:

- [Related session-aware playback implementation](https://android.googlesource.com/kernel/msm/+/android-7.1.0_r0.2/sound/soc/msm/qdsp6v2/q6voice.c)
- [Related session iterator and subscription mapping](https://android.googlesource.com/kernel/msm/+/9d815ef2c3a51c0bc6f1558b4c8447e471bf27cb/sound/soc/msm/qdsp6v2/q6voice.c)

A kernel modification or another supported native interface to the active session may be required. This diagnosis does not establish a stock KaiOS app-only solution. The upstream GerdaOS kernel comparison below found the same session-selection omission.

## GerdaOS comparison

Read-only inspection of the upstream repository's `src/boot/8110/zImage` on 2026-10-12 found SHA-256 `2ce93b48c6195718f5bbc13f74e3cf847e6b01c405fcd95ee020ec766c5fe45e`. Its embedded kernel version is `3.10.49-ged30a3a-00776-g1f10757`, built 2018-10-26.

The stock playback function's first 24 instruction bytes have one matching location in the decompressed Gerda image, at virtual address `0xc06eceb4`. Inspection of its complete 268-byte function shows the same branches, state offsets, handle check and session-name choices as stock. Its first playback port selects `Voice session`, the second selects `Voice2 session`, and its fallback selects `VoLTE session`. VoiceMMode sessions are again omitted. Differences in the instruction body are call relocations, not session-selection changes. The disassembly and binary are saved under `data/audio-probe/gerda-review/`.

The build recipe repacks this prebuilt `zImage` with a ramdisk; it does not compile a corrected audio driver. The boot properties enable root/debug access and persistent ADB. Those defaults and editable startup services make Gerda a convenient base for a boot agent and custom kernel, but the inspected bundled kernel does not address the diagnosed omission. This comparison applies to the upstream repository image examined, not every historical or third-party Gerda build. No Gerda image was installed or booted on the phone during this review.

- [Inspected upstream Gerda kernel](https://gitlab.com/project-pris/system/-/blob/master/src/boot/8110/zImage)
- [Gerda boot image recipe](https://gitlab.com/project-pris/system/-/raw/master/Makefile)
- [Gerda boot properties](https://gitlab.com/project-pris/system/-/raw/master/src/boot/8110/root/default.prop)
- [Gerda startup service definitions](https://gitlab.com/project-pris/system/-/raw/master/src/boot/8110/root/init.b2g.rc)

The recommended direction remains a targeted native/session correction, integrated into a custom gateway image if needed. A standard Gerda migration by itself is unlikely to make the tested injection path audible on VoiceMMode calls.

Before deploying any firmware change, obtain a fresh recoverable backup including userdata, document boot/partition compatibility and restoration, and prepare a concrete image for review. The current session has not authorized flashing.

After a corrected native path is available, prove audible injection into a normal active call on each SIM, simultaneous capture and injection, bounded start/stop behavior, call stability and restart recovery. USB transport and remote WebRTC follow that proof.

## Custom implementation prepared

The owner approved preparation of a custom Gerda build. The owned replacement
is [kaiforward_incall_playback.h](firmware/kernel/kaiforward_incall_playback.h).
It selects the matching VoiceMMode session per playback port, retains legacy
and VoLTE behavior, includes VoWLAN on the first port, keeps session locking
and pending handover state, checks reference-count overflow/underflow, and
reports `ENODEV` when an enable request has no active CVS session. Negative
native helper errors are retained. The existing native command helpers are
unchanged; their timeout handling can still return zero, so diagnostic return
codes alone do not establish a modem acknowledgement or audible playback.

The Nokia source is pinned at `fa91dd51a1239cdaad2be3c21f3835ec56a40669` and
compiled with Android GCC 4.8 and the saved live config, producing release
`3.10.49-kaiforward+`. Disassembly of the completed kernel confirms both
VoiceMMode VSID constants in the corrected selection code. The C fixture
tests session isolation, legacy selection, pending handover state, accounting,
locking and error paths. Rebuilt modules, including Wi-Fi, are checked against
the kernel's release and exported symbol CRCs; a deliberately mismatched CRC
was correctly rejected by validation.

Build instructions, image/recovery scope, and remaining installation gates
are in [firmware/README.md](firmware/README.md). The initial image failed to boot; it had the same `0700` ramdisk traversal
bug later identified in the native UI trial. On 2026-10-13 an isolated update
retained the working native ramdisk byte for byte and changed kernel/modules.
The corrected kernel booted, Wi-Fi and SIM registration returned, and the
owner confirmed audible PC microphone injection alongside caller capture on
a live SIM 2 call. The call used VoiceMMode1. Other SIM/session combinations
and long unattended operation remain unverified.

## Evidence

Private raw evidence is under `data/audio-probe/`:

- `downlink-20261012-165925-9764.wav` and its signal/mixer reports.
- `injection-test.wav`, the exact PC-verified source file.
- `injection-retry-progress.txt` and `injection-half-gain-progress.txt`.
- Mixer/PCM snapshots with prefixes `injection-voice1-20261012-172522-5674` and `injection-voice1-20261012-173431-6370`.
- `live-boot-sha256.txt`, `live-kernel-symbols-addresses.txt` and `stock-kernel-image.bin`.
- `stock-voc-start-playback.disassembly.txt`, `stock-voc-get-session-id.disassembly.txt` and `stock-voice-cvs-start-playback.disassembly.txt`.
- `after-half-gain-kernel-and-state.txt`; no injection-specific error in this log established the cause of silence.

The probe scripts remain experiments, not a production bridge. Playback exit code 0 is not proof that the caller heard the audio.
