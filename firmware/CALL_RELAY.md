# Native browser call relay

Open **http://127.0.0.1:8768** on this PC. SIM 2 is the currently inserted SIM.
When a call arrives, click **Answer** and allow microphone access. The caller
plays through the computer's audio output; the computer microphone feeds the
cellular call. Click **Hang up** to end it. **Enable audio** attaches computer
audio to an already connected call; **Disable audio** releases the microphone
and audio streams while retaining the call. The number field and **Call**
button send a dial request on the selected SIM. Outgoing dial framing is
implemented; an outgoing carrier call has not yet been manually tested.

The phone and PC must be on the same Wi-Fi, and this PC must remain awake.
USB is useful for maintenance but is not in the call audio or control path.
The browser page is local to this PC. Audio is streamed live and is not saved
to the SMS archive or an audio recording. The SMS viewer remains at
**http://127.0.0.1:8766**.

On 2026-10-13 the owner called SIM 2 from another phone, answered through this
page, confirmed audio in both directions and confirmed browser hangup.
The test transported 2,884 caller-audio packets and 2,901 microphone packets,
each containing 20 ms of mono audio. After hangup, both PCM endpoints closed,
the capture and both injection mixer controls were Off, audio policy returned
to normal mode, and the phone reported idle with no queued SMS. This proves
one simultaneous capture/injection session on SIM 2 using VoiceMMode1.
SIM 1 is empty; calling on that SIM, call waiting, multiple calls, handovers
and long unattended operation remain unverified. Controls refuse multiple
calls rather than choosing an ambiguous call to answer or end.

## Components

- `firmware/native/call.c` adds a root-private Unix control socket to the
  existing native radio service. It does not connect another client to RIL
  or displace SMS reception. Answer and hangup use modem-confirmed responses;
  uncertain results are never automatically replayed.
- `firmware/kernel/kaiforward_incall_playback.h` corrects playback selection
  to include VoiceMMode1/2. The original Gerda playback path omitted the
  session used by this phone, allowing silent playback despite a success
  return. The corrected kernel is `3.10.49-kaiforward+`, with Gerda's original
  46 appended device trees and matching rebuilt modules, including Wi-Fi.
- `firmware/relay/relay.c` is a static ARM helper started by `kf-relay` under
  init. It connects outbound over verified TLS, authenticates using the
  existing private receiver device ID/token, reports call state and carries
  audio and call commands. It derives the server host from the configured
  SMS receiver URL; its default call port is 8769. `--server`, `--port`,
  `--ca` and `--home` allow explicit provisioning.
- `firmware/relay/pcm.c` sets Android's native call-audio mode and opens
  MultiMedia1 downlink capture at 8 kHz and MultiMedia2 modem injection at
  48 kHz. The session follows the running native voice PCM, not the physical
  SIM number. Capture and injection run concurrently; the native helper
  restores its routes on hangup, audio detach or connection loss. It refuses
  routes/streams already occupied by another client.
- `server/call_relay.py` serves the local browser and authenticated phone TLS
  endpoint. The phone side uses framed PCM/commands; the browser uses a local
  WebSocket and AudioWorklet. Each direction carries mono 8 kHz, 16-bit audio
  in 20 ms frames. Queues are bounded and old audio is dropped rather than
  building an ever-growing delay. Browser origin/Host checks restrict control
  to the local page, and one browser owns the audio stream at a time.

The PC service is `kaiforward-call-relay.service`, enabled for this user:

```sh
systemctl --user status kaiforward-call-relay
systemctl --user restart kaiforward-call-relay
```

Its launch flags configure the phone listener address/port, browser port,
receiver configuration and TLS certificate/key. Defaults on this PC are
`192.168.0.122:8769` for the phone and `127.0.0.1:8768` for the browser.
The private phone trust certificate is `/data/kaiforward/ca.pem`.
The helper expects an acknowledgement of each status update and stops its
streams after eight seconds without incoming relay traffic. TCP loss also
closes the session; reconnection does not resend a cellular command.

## Firmware and recovery

The full credential-free image is `dist/gerda-native-calls-20261013`.
Its signed installer is staged as **18-install-native-calls.zip** on the
physical microSD. It writes boot/system, preserves userdata/configuration,
and contains both native helpers, the corrected kernel, rebuilt modules and
native startup. **19-native-call-startup.zip** changes only boot and requires
the matching helpers/modules already installed in system.

**17-restore-native-kernel.zip** restores the working Gerda kernel, native
ramdisk and original modules. It restores the earlier SMS/status setup and
does not start the call relay. **15-install-native-permission.zip** is the
earlier complete SMS/status installer. Original stock recovery packages and
the owner-confirmed userdata/cache backup remain preserved. No modem,
calibration, recovery or bootloader partition was changed for the call relay.

The earlier audio kernel installation also contained `0700` ramdisk root and
mount directories. The successful isolated trial preserved the now-working
native ramdisk byte for byte, changed only kernel/modules, and verified boot
partition readback. The subsequent startup update retains that corrected
kernel and adds the relay init service. The complete image is assembled from
those tested components; the full system image has not itself been flashed.

## Building and checks

The native core and call helper use the pinned Zig/SQLite inputs already
documented in [NATIVE_UI.md](NATIVE_UI.md). The call helper additionally uses
Mbed TLS 3.6.7, whose official archive SHA-256 is checked before extraction.
Its source and license are under `data/call-relay/mbedtls-3.6.7`.

```sh
python firmware/scripts/build_native.py --output dist/native-runtime-call
python firmware/scripts/build_call_relay.py
# Prepare the reviewed kernel/modules using the saved working boot/modules:
GATEWAY_JAVA=/path/to/java8 python firmware/scripts/prepare_call_kernel.py \
  --original-boot dist/gerda-native-permission-startup/boot.img \
  --original-modules data/call-relay/original-modules \
  --output dist/new-call-kernel
python firmware/scripts/build_native_image.py \
  --runtime dist/native-runtime-call --call-kernel dist/new-call-kernel \
  --relay-runtime dist/native-call-relay --output dist/new-native-calls
python -m unittest discover -s tests -q
npm test
```

Build/package tools only prepare files on the PC. Package directories must
be new/empty. Kernel/module ABI checks, boot layout/DTB preservation,
filesystem and executable-mode checks and signed package verification passed.
The full suite passed 57 Python tests and 10 JavaScript tests. New tests cover
SMS receipt during an answer request, an exact hangup index, invalid/multiple
call states, authentication, fragmented/bounded transport frames, exclusive
audio attachment and uncertain results without command replay.
