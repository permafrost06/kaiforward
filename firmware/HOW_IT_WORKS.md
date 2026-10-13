This follows the native SMS and browser call implementation installed on the
Nokia 8110 4G. The phone runs native C services; the PC runs Python servers
and JavaScript browser interfaces. Code excerpts below omit surrounding
parsing/error handling where indicated.

1. **Boot starts the native programs.**

   The bootloader loads the Linux kernel and its initial filesystem, called
   the ramdisk. Android's `init` reads the service definitions. Gecko/B2G,
   `rilproxy` and the API daemon are disabled at startup; the native UI and
   radio client take their places. Several processes use the same
   `kaiforward-native` binary with different arguments. Call audio uses a
   separate `kaiforward-relay` binary.

   [init.native.rc](native/init.native.rc) declares the roles:

   ```ini
   service kaiforward-sms /system/bin/kaiforward-native
       class main
       user root
       group root radio log

   service kf-relay /system/bin/kaiforward-relay
       class main
       user root
       group root audio inet
   ```

   Separating UI, radio/SMS, upload, Wi-Fi and audio lets them work
   independently. A slow upload does not occupy the radio event loop.
   Root access supplies Linux device/file access that the earlier certified
   KaiOS app obtained through its platform permissions.

2. **The keypad interface draws pixels directly.**

   [ui.c](native/ui.c) maps `/dev/graphics/fb0`, the display framebuffer, and
   reads key events from `/dev/input/event*`. It draws the font, status screen
   and settings menu in C. Battery percentage comes from the kernel's battery
   interface; SIM/call/message state comes from files written by the services.

   ```c
   int fb = open("/dev/graphics/fb0", O_RDWR);
   // Read framebuffer geometry, then map its pixel memory:
   unsigned char *mem = mmap(NULL, fix.smem_len,
                            PROT_READ | PROT_WRITE, MAP_SHARED, fb, 0);
   ```

   The screen wakes for an incoming call or newly received SMS and blanks
   after 30 seconds of inactivity. This implements display blanking rather
   than full phone suspend. [common.c](native/common.c) stores settings
   atomically in `/data/kaiforward/config`. Wi-Fi password, receiver URL,
   access token and device ID persist through reboot. Password/token editing
   displays the actual characters.

3. **The native radio service talks to the existing cellular stack.**

   The original modem firmware still handles cellular signaling and radio
   transmission. Gerda's Qualcomm radio libraries and `rild` translate
   modem communication into Android Radio Interface Layer (RIL) messages.
   [radio.c](native/radio.c) connects to `/dev/socket/rild` and `rild2`, one
   interface per SIM slot, and parses length-prefixed Android parcels.

   It requests SIM/app status and network registration and processes
   unsolicited events: new SMS, call-state changes and registration changes.
   SIM labels reflect those responses. The native radio process owns these
   connections; other helpers ask it to perform operations.

4. **Wi-Fi connects and establishes IP routing.**

   [wifi.sh](native/wifi.sh) creates the supplicant configuration from saved
   settings, brings up `wlan0`, launches `wpa_supplicant` for association and
   authentication, and launches BusyBox `udhcpc` for DHCP. Network scanning
   uses the supplicant control interface through [wifi.c](native/wifi.c).

   [udhcpc.sh](native/udhcpc.sh) applies the leased IP/subnet and tells Android
   `netd` about the interface, local/default routes and DNS servers. Wi-Fi
   association alone would not give the native programs a usable routed
   network. The supervisor reconnects when settings or its child processes
   change. The phone initiates its connections to the PC.

5. **An SMS is saved before the modem gets a success acknowledgement.**

   The modem delivers an encoded SMS PDU. [sms.c](native/sms.c) decodes its
   sender, timestamp and text, including GSM/UCS-2 encoding. [store.c](native/store.c)
   retains the original PDU and SIM slot, assembles multipart messages and
   stores completed messages in `/data/kaiforward/native.sqlite3`.

   The reception branch in `radio.c` orders the work deliberately:

   ```c
   int rc = store_pdu(db, r->slot, pdu);
   ack(r, rc == 0);
   ```

   `store_pdu` commits its transaction before returning success. The SQLite
   settings are:

   ```sql
   PRAGMA journal_mode=WAL;
   PRAGMA synchronous=FULL;
   ```

   That provides a durable local copy before a positive acknowledgement.
   Unsupported records stay available for review; incomplete multipart
   messages wait for the remaining parts. Received counts completed received
   messages; Queued counts records still awaiting upload. Uploaded rows remain
   on the phone, so Received does not disappear when Queued becomes zero.

6. **SMS upload gets a second acknowledgement from the PC.**

   [upload.c](native/upload.c) builds JSON and calls the phone's native `curl`
   with a private configuration file. The installed destination is
   `https://192.168.0.122:8767/v1/messages`. HTTPS verifies the PC certificate;
   the bearer token authenticates the phone. The uploader checks the exact
   accepted message key in the response:

   ```c
   if (extra != EOF || bad || memchr(reply, 0, size) || !ack_valid(reply, key))
       return -1;
   return store_ack(db, id) ? -1 : 1;
   ```

   [receiver.py](../server/receiver.py) validates the token, device ID and
   records, commits them to `data/sms.sqlite3`, then returns accepted keys.
   Its unique `(device_id, message_key)` key makes identical retries safe.
   A conflicting body under an existing key is rejected.

   If Wi-Fi/PC delivery fails, the phone retains the pending record and
   retries. Sync requests an immediate attempt. Native upload IDs have a
   prefix offset to coexist with the earlier KaiOS-app archive. The modem's
   acknowledgement means "saved on phone"; the PC's means "archived on PC".
   [viewer.py](../server/viewer.py) and [App.tsx](../viewer-ui/src/App.tsx)
   read that archive for the SMS page at `http://127.0.0.1:8766`.

7. **The call helper establishes an authenticated encrypted session.**

   [relay.c](relay/relay.c) loads the same private device ID/token and derives
   the host from the configured SMS receiver URL. The call port defaults to
   8769. It uses Mbed TLS to verify the PC certificate and hostname before
   sending credentials:

   ```c
   mbedtls_ssl_conf_authmode(&options, MBEDTLS_SSL_VERIFY_REQUIRED);
   mbedtls_ssl_conf_ca_chain(&options, &certificates, NULL);
   ```

   [call_relay.py](../server/call_relay.py) checks the credentials and serves
   the browser page locally at `http://127.0.0.1:8768`. The browser does not
   receive the phone's authentication token. Phone state is reported about
   once per second. Each transport frame has a four-byte length, a one-byte
   type and its payload:

   ```python
   return struct.pack('>I', len(payload) + 1) + bytes([kind]) + payload
   ```

   Types identify authentication, state, commands/results, audio start/stop,
   audio in either direction and heartbeat acknowledgements. Lengths let
   readers reconstruct messages even when TCP splits or combines reads.

8. **Incoming-call notification and Answer use the existing radio owner.**

   A RIL call-state event causes the native radio service to request the
   current call list. It keeps call count, state and index. The relay reports
   that state to the PC, which updates the selected SIM in the browser.

   [app.js](../server/call-ui/app.js) requests microphone permission when you
   click Answer, attaches browser audio, and sends the answer operation. The
   PC assigns an operation ID and sends it over TLS. The phone helper passes
   it through `/data/kaiforward/call.sock`, implemented by
   [call.c](native/call.c). This socket is private to root/the service owner.

   `radio_call` validates the actual state and issues RIL requests:

   ```c
   if (action == 1 && r->calls == 1 && r->call_state == 4)
       code = 40; /* RIL_REQUEST_ANSWER */
   else if (action == 2 && r->calls == 1 && r->call_index > 0) {
       code = 12; /* RIL_REQUEST_HANGUP, one explicit call index */
       put(args, 1);
       put(args + 4, r->call_index);
       size = 8;
   }
   ```

   Responses return through the same operation ID. A timeout/disconnect
   produces an uncertain result; dial/answer/hangup commands are never
   automatically replayed. Multiple calls are rejected by these controls.

9. **The native Android audio stack enters call mode.**

   [pcm.c](relay/pcm.c) asks the existing audio-policy service to enter
   `MODE_IN_CALL` (2), and later restores normal mode (0). Binder is Android's
   local interprocess communication mechanism; this request uses it:

   ```c
   utility("/system/bin/service", "call", "media.audio_policy", "3", "i32",
           in_call ? "2" : "0", output, sizeof(output));
   ```

   The media/audio HAL then activates the cellular voice audio path. Two
   dependencies removed with Gecko had to be supplied natively:
   `kf-display` supplies SurfaceFlinger, which media initialization waits for;
   [permission.c](native/permission.c) supplies a small Binder permission
   controller granting only the trusted stack's `MODIFY_AUDIO_SETTINGS`.
   The missing services previously blocked audio setup and the RIL event
   thread, leaving hangup/SMS events queued.

10. **The corrected kernel selects the live voice session.**

    Native audio sessions have names independent of SIM-slot numbers. The
    SIM 2 test used VoiceMMode1. The original Gerda playback function selected
    legacy Voice/Voice2/VoLTE sessions and omitted VoiceMMode1/2. It could
    return success while starting playback on no active call.

    [kaiforward_incall_playback.h](kernel/kaiforward_incall_playback.h)
    includes the missing sessions while preserving existing choices:

    ```c
    if (port_id == VOICE_PLAYBACK_TX)
        return session_id == VOICE_SESSION_VSID ||
               session_id == VOLTE_SESSION_VSID ||
               session_id == VOWLAN_SESSION_VSID ||
               session_id == VOICEMMODE1_VSID;
    if (port_id == VOICE2_PLAYBACK_TX)
        return session_id == VOICE2_SESSION_VSID ||
               session_id == VOICEMMODE2_VSID;
    ```

    Selection is based on the running voice PCM. Locking and reference counts
    coordinate starting/stopping playback and pending handover state. The
    installed kernel is `3.10.49-kaiforward+`; modem firmware/calibration are
    preserved.

11. **Caller audio is captured and played by the browser.**

    After browser audio is attached and the selected call is active, the PC
    sends START. The phone checks for a single active voice session and free
    multimedia routes, enables `MultiMedia1 Mixer VOC_REC_DL`, and opens
    `/dev/snd/pcmC0D0c`. This captures the cellular downlink at 8 kHz mono,
    16-bit PCM. PCM is a sequence of numerical sound samples.

    Each 20 ms frame has 160 samples / 320 bytes. A capture child reads ALSA
    PCM while a separate playback child handles the other direction. The
    parent carries frames over TLS. The PC forwards the downlink to the audio
    owner browser. [audio-worklet.js](../server/call-ui/audio-worklet.js)
    buffers roughly 60 ms initially, converts to the browser output rate and
    plays through computer speakers/headphones. Old buffered audio is dropped
    if delay grows too large. No audio recording is written.

12. **The PC microphone supplies the cellular uplink.**

    The browser microphone uses echo cancellation, noise suppression and
    automatic gain. Its AudioWorklet converts to 8 kHz signed 16-bit samples
    and sends a frame every 160 samples:

    ```js
    if (this.used === 160) {
      const buffer = this.packet.buffer;
      this.port.postMessage(buffer, [buffer]);
      this.packet = new Int16Array(160); this.used = 0;
    }
    ```

    The browser WebSocket carries these bytes locally to the Python server;
    TLS carries them to the phone. The playback child converts each frame
    into 960 samples because the injection PCM expects 48 kHz:

    ```c
    for (int i = 0; i < 160; i++) {
        for (int j = 0; j < 6; j++)
            output[i * 6 + j] = last + (input[i] - last) * (j + 1) / 6;
        last = input[i];
    }
    ```

    It writes `/dev/snd/pcmC0D1p` through the selected In-call Music mixer.
    The corrected kernel routes that digital stream into the active voice
    session for cellular transmission. Conversion to 48 kHz matches the
    device interface; it does not add detail lost at 8 kHz.

13. **Hangup and connection loss release the audio resources.**

    Browser Hang up sends RIL request 12 for the current call index. A caller
    hangup arrives as a radio event and updates the call list. The helper
    stops both PCM children, closes pipes/PCM streams, switches its capture
    and injection routes Off and restores normal audio mode when the call
    ends. Disable audio or closing the browser releases relay audio while
    retaining the cellular call.

    The PC acknowledges status updates; the helper exits the session if it
    receives no relay traffic for eight seconds. It cleans up on connection
    loss and attempts reconnection. Commands are not replayed; old audio is
    discarded. SMS pending records stay on disk until acknowledged by the PC.

14. **Build and recovery files make the installation reproducible.**

    [build_native.py](scripts/build_native.py) cross-compiles the core for
    ARM Cortex-A7 with SQLite; [build_call_relay.py](scripts/build_call_relay.py)
    builds the static call helper with checksum-verified Mbed TLS.
    [prepare_call_kernel.py](scripts/prepare_call_kernel.py) combines the
    reviewed kernel with Gerda's original 46 device trees and checks rebuilt
    module compatibility. Wi-Fi modules must match the kernel's release and
    exported symbol versions.

    [build_native_image.py](scripts/build_native_image.py) assembles boot/system
    images and validates native startup, file hashes, executable permissions
    and filesystem consistency. [package_recovery.py](scripts/package_recovery.py)
    creates the signed complete installer. The early recovery loop came from
    host staging permissions accidentally making ramdisk root/mount points
    `0700`; these must be `0755` so system/radio services can traverse them.

    `18-install-native-calls.zip` is the complete boot/system package;
    `19-native-call-startup.zip` is the startup-only update requiring matching
    installed helpers/modules. `17-restore-native-kernel.zip` restores the
    earlier native Gerda SMS/status kernel/modules. The live installation was
    updated and readback-checked in stages; the assembled full system image
    has not itself been flashed. Settings/SMS live under `/data`, preserved by
    these firmware updates.

The PC services use systemd user units in `deploy/systemd/`, starting with the
user session and restarting on failure. The phone services start at boot.
The PC must remain awake and reachable for live calls. USB/ADB is the
maintenance/build-install route; audio and SMS use Wi-Fi in this setup.

Useful data locations are `/data/kaiforward/native.sqlite3` on the phone,
`data/sms.sqlite3` on this PC, `/data/kaiforward/config` for phone settings,
`data/config.json` for receiver identity/token and
`data/native-receiver/` for the PC certificate/key. The certificate trusted
by the phone is `/data/kaiforward/ca.pem`.

The owner confirmed two-way browser audio and browser hangup on SIM 2.
Automatic phone relay startup/reconnection after reboot and resource cleanup
were verified. 57 Python and 10 JavaScript tests passed. SIM 1 calling,
outgoing carrier dialing, multiple calls, handovers and long unattended
operation remain unverified. The phone's native keypad remains a status/settings
interface; browser call controls provide the tested answering/hangup workflow.
