# Native Gerda UI and SMS forwarding

This experimental variant replaces the running Gecko/KaiOS interface with a
small C framebuffer UI and replaces KaiForward's KaiOS app with native init
services. Wi-Fi, its password, the complete receiver URL, receiver token and
device ID are configurable on the keypad or from a PC over USB. Settings and
the SMS queue live in `/data/kaiforward` and survive reboots. The phone now also
runs the [native browser call relay](CALL_RELAY.md), with a corrected audio
kernel. Latest complete firmware is `dist/gerda-native-calls-20261013`, staged
as `18-install-native-calls.zip`. The SMS bootstrap history below used the
original Gerda kernel; the call relay retains its device trees while replacing
the kernel/modules and adding the boot-started call helper.

The native firmware is installed on the TA-1059. The first build entered
recovery because the host's private staging-directory mode became the phone's
root directory mode (`0700`), preventing non-root critical services from
starting. The image builders now force root and mountpoint modes to `0755`.
The corrected boot image was written with target-size, original-image hash,
payload hash and full partition readback checks. A full reboot subsequently
started the native screen, radio clients, Wi-Fi supervisor and uploader.

Wi-Fi scanning, WPA2 association, DHCP, netd routing/DNS and verified HTTPS
access to the PC receiver passed on the actual phone, including automatic
reconnection after reboot. An isolated synthetic SMS also passed the actual
ARM runtime, durable acknowledgement, receiver-outage retention and Wi-Fi
upload without changing the production archive. Later owner-sent carrier
SMSes on SIM 2 were durably received and acknowledged, forwarded over Wi-Fi and
confirmed in the production PC archive. The UI now preserves a Received count
alongside the pending queue, wakes for new SMS, and displays a short receipt
notice. An empty queue after successful forwarding no longer hides reception.

After a later restart, SIM 1 correctly reported absent and SIM 2 registered.
The prior duplicate presence report is no longer observed. The owner confirmed
that SIM 1 was removed while powered off.

Incoming-call setup exposed another dependency: mediaserver's camera startup
waits for the Binder SurfaceFlinger service normally supplied by Gecko. That
kept the audio Binder thread pool from starting; QCRIL then blocked on audio
setup and stopped servicing further radio notifications. Native startup now
launches the existing Gerda SurfaceFlinger binary as `kf-display`, without
Gecko, and stops its boot animation. Audio policy and call-list queries have
been verified after a full reboot. The status UI queries calls on call-state
notifications, shows an incoming-call banner and wakes its display. This is
status reporting; answering, rejecting, dialling and a ringtone are not part
of this minimal UI. The first reported call's blocked state was recovered by
restarting RIL2 after the media dependency was available.

A subsequent incoming call reached the screen, but hangup and a later SMS
stalled. Read-only thread backtraces showed QCRIL waiting in
`AudioSystem::setParameters` and mediaserver waiting in `checkPermission` for
Gecko's missing Binder permission controller. The new native `kf-permission`
service supplies that controller. It grants only
`android.permission.MODIFY_AUDIO_SETTINGS` when requested by root/system/media
for root/system/radio/media UIDs; other permissions and app callers are denied.
It uses Android's 32-bit Binder protocol 7 directly, requires no KaiOS app,
and runs as the system UID. Registering it released the existing blocked
threads without restarting RIL: the hangup cleared, the pending carrier SMS
was received, and the PC confirmed its Wi-Fi upload. Both native Binder
providers and SIM registration were verified after a full reboot. The owner
then confirmed that an incoming call cleared when the caller hung up and
Received reached 3 after the SMS test. The phone reports Call idle, three
uploaded messages and an empty queue; all three are confirmed in the PC archive.

Gerda's bundled modem image differs from the installed original modem image
(SHA-256 values recorded in the private hardware audit). The native variant
uses Gerda's kernel and radio/system libraries while preserving the original
modem firmware and device calibration. No modem change was needed for the
confirmed SMS reception. The observed call stall was traced to the missing
display and permission services. Modem-version incompatibility has
not been established.

The earlier SMS-only credential-free images and signed installer are in
`dist/gerda-native-20261013-permission`. The standalone ARM binary and payload
are in `dist/native-runtime`. That SMS-only installer is
`15-install-native-permission.zip`; `14-native-permission-startup.zip` supplies
only the latest boot configuration and requires the matching native runtime
already installed in system. Earlier builds and installers, including
`13-install-native-status.zip`, are superseded. Both latest packages preserve
modem and calibration partitions. The faulty original was renamed
`06-install-native-SUPERSEDED.zip`.

The live repair installed the new native binary and boot configuration with
original-image, target-size, payload and full boot-partition readback checks.
The original Gerda kernel/device trees are unchanged. The full recovery
package writes only boot/system and requests no factory reset. No modem,
radio calibration, recovery or bootloader partitions were changed.

The owner completed `07-backup-pre-native.zip` and explicitly waived copying
and verifying that fresh userdata/cache backup on the PC. It remains on the
microSD in `kaiforward-pre-native-20261013`. The previously verified original
stock boot/system images and `05-restore-stock-sd.zip` remain available.

## Phone controls

The 240×320 home screen shows battery, Wi-Fi carrier status, both SIMs,
messages waiting for upload and messages needing review. “Queue empty” means
there is nothing pending; it is not a receiver reachability test.

- Left soft key: Sync immediately.
- Centre/OK: Settings. Up/down selects a field; OK edits it.
- Wi-Fi network: up/down selects a scanned network; OK chooses it and opens
  password entry for a secured network. `1` scans again; the left soft key
  opens manual SSID entry. Open networks connect without a password.
  `+` marks WPA personal security; `!` marks unsupported security.
- Right soft key: Back or Cancel. On the home screen it turns the display off.
- Number keys in an editor: multi-tap text, including numbers.
- `*`: cycle URL/password symbols. `#`: lower case, upper case, numeric mode.
- End/power key in an editor: delete the last character.
- Left soft key or OK in an editor: save. Invalid settings are rejected.

Passwords and tokens are visible while editing, so typed characters can be
checked on the phone. An idle display blanks after 30 seconds;
pressing a key wakes it. This is display blanking, not a complete suspend or
battery-management implementation. Non-ASCII SSIDs can be provisioned from
the PC; the minimal screen font and keypad editor cover ASCII.

## PC setup and configuration

The receiver accepts the same `/v1/messages` protocol as the stock app. Its
default binding remains `127.0.0.1`; Wi-Fi requires an explicitly reachable
address. `--host` selects the receiver's bind address and `--tls-cert` plus
`--tls-key` enables HTTPS. For example, with a certificate valid for the PC's
chosen address:

```sh
python3 server/receiver.py --host 192.168.0.122 --port 8765 \
  --tls-cert /path/to/receiver-certificate.pem \
  --tls-key /path/to/receiver-key.pem
```

Use the actual PC address. The existing receiver systemd service still uses
its original localhost binding. Update that service's `ExecStart` for a
persistent Wi-Fi deployment; do not start two listeners on the same address
and port. A local HTTP receiver is also supported when the configured URL
starts with `http://`; HTTPS protects the SMS bodies and bearer token on the
network. The browser viewer remains local and read-only.

For the authorized native trial, an additional systemd user service,
`kaiforward-native-receiver`, now listens at
`https://192.168.0.122:8767/v1/messages`. It uses the existing private archive
and device token, and a locally generated certificate covering the LAN address.
Its private key and certificate are under `data/native-receiver`. The original
localhost receiver remains active. Bind address and port are configurable in
`data/native-receiver/environment`; restart the service after editing them.
An address change also requires a certificate covering the new address and
an updated phone destination/CA.

Prepare phone settings without connecting to or changing the phone:

```sh
python3 firmware/scripts/configure_native.py \
  --ssid 'Your Wi-Fi network' \
  --receiver-url 'https://192.168.0.122:8765/v1/messages' \
  --ca /path/to/receiver-certificate.pem
```

The tool prompts for the Wi-Fi password without echoing it. It reads the
receiver token and device ID from `data/config.json` by default. For another
receiver, use `--receiver-config` pointing to a private JSON file containing
those two values; if none exists, it prompts for the token. It saves private
files in `data/native-settings` and prints no credential values.

After a native image is installed and its root ADB connection works, add
`--install --serial 22aaa475` to copy the settings and CA certificate over USB.
The tool refuses a non-root phone or one without the native binary. It writes
no partitions and does not install a KaiOS application. Settings can then
be changed in the native menu. The device ID must match the selected receiver.
Changing to a private HTTPS receiver may also require provisioning its CA;
the keypad menu does not edit certificates.

WPA/WPA2 personal networks and open networks are supported by the existing
Gerda supplicant. The Wi-Fi supervisor loads the existing WLAN module, brings
up `wlan0`, supervises supplicant/DHCP and retries after child failures.
A root BusyBox udhcpc process and its DHCP hook configure Android 6 netd
routing and DNS, which Gecko normally handled. The supplicant configuration
is private to the Wi-Fi UID under `/data/misc/wifi`; keeping it in the
root-only SMS directory prevented the supplicant from reading it. The
supervisor checks child states so zombie processes are restarted. WPA3 and enterprise Wi-Fi configuration are outside this interface.

## Native SMS behavior

`kaiforward-sms` owns `/dev/socket/rild` and `/dev/socket/rild2`; the Gecko
process and `rilproxy` are disabled to avoid competing clients. Qualcomm's
modem, RIL, firmware-loading and calibration services remain in the system.
The daemon turns an off radio on, queries SIM/registration state, and can
activate an unselected ready SIM subscription. A SIM requiring PIN/PUK is
reported as locked; this UI does not unlock it.

An incoming SMS PDU is committed to SQLite with FULL synchronization before
the daemon gives a positive modem acknowledgement. A failed commit receives
a negative acknowledgement. Transport replays are deduplicated per SIM and
PDU. GSM default/extension alphabet and UTF-16 text are decoded, including
concatenation headers with 8-bit and 16-bit references. Multipart messages
are queued only after all parts arrive, even across restarts. Matching uses
SIM, sender, reference, encoding, part count and a ten-minute timestamp window;
conflicting segments are kept for review. Raw PDUs are retained, including
unsupported binary/port-addressed/national-alphabet/CDMA messages. Those
unsupported messages are not uploaded as invented text. Multipart reception
and reference reuse still need carrier testing.

SIM-stored message notifications read EF_SMS with SIM/USIM path and AID
selection. SIM records are not deleted. This build does not scan/import the
old Gecko IndexedDB inbox or all existing SIM records; the previous PC archive
remains available. MMS and call/USSD controls are not implemented in this UI.

`kf-upload` runs independently and receives an immediate wake request
when the forwarding queue grows. Each request contains one SMS. Only the
exact receiver acknowledgement marks that record uploaded; lost replies are
replayed safely. Uploads time out, back off to at most five minutes and can
be retried immediately with Sync. HTTP conflicts, authentication failures,
untrusted certificates and unavailable networks retain the queue.
No redirects or disabled certificate verification are enabled. TLS uses a
provisioned CA file or the public CA bundle included in the image.

The existing native Gerda `curl` provides transport; it is an older TLS client,
so compatibility with the intended HTTPS server requires a device test.
Wi-Fi carrier status does not prove DHCP/DNS or server reachability. The
native process uses monotonic deadlines; valid phone clock time is also
required for certificate verification and outgoing message timestamps.

Native IDs occupy a separate range starting at `1000000001`, and messages
include `sim_slot` (1 or 2). The PC receiver preserves this optional field
while accepting old app records. The viewer's existing Sent/Received filters
continue to work.

## Sending and archiving SMS natively

There is no text composer on the status screen. The native command-line
interface can queue a send on a selected SIM:

```sh
/system/bin/kaiforward-native --send 1 +8801712345678 'Message text'
/system/bin/kaiforward-native --outbox
```

Run these on the installed phone's root shell. `--send` is a real send command
and can incur the normal carrier SMS charge. It supports a numeric recipient
and one UTF-16 segment (up to 70 code units). The daemon sends only when the
selected SIM is ready and registered. A successful modem response puts the
SMS into the same durable forwarding queue with direction `sent` and the
recipient. “Sent” means accepted by the modem/network, not recipient delivery.

Failed sends are not forwarded as successful. A restart/disconnect during
submission marks the job `uncertain`; it is not automatically sent again.
The original text remains in the private outbox for inspection. `--outbox`
prints job IDs, SIMs and statuses without message bodies or phone numbers.
Do not start a second SMS daemon alongside the init service.

## Building and checking

```sh
python3 firmware/scripts/build_native.py
python3 firmware/scripts/build_native_image.py --output dist/gerda-native-new
python3 firmware/scripts/build_native_image.py \
  --output dist/gerda-native-new --validate-only
python3 -m unittest discover -s tests -p test_native.py -v
```

The build uses pinned, checksum-verified Zig 0.15.2 and SQLite 3.50.4 inputs.
The automatic compiler download targets this aarch64 Linux PC; another host
can supply `--zig` and `--sqlite`. The result is a static ARM Cortex-A7/Linux
binary, with no Gecko, Node, Python or dynamic Bionic dependency for the UI,
radio client or queue. SQLite is public domain; the embedded font has its
license in `native/FONT-LICENSE.txt`. Build manifests record native source and
binary hashes. The image builder checks the pinned Gerda revision, disables
the three browser services and validates the generated boot layout, imported
init definitions, installed payload contents, executable modes and ext4
filesystem. The current full image additionally requires the prepared call
kernel/modules and relay runtime described in [CALL_RELAY.md](CALL_RELAY.md).

Twenty-two native runtime tests cover Unicode/timezone decoding, multipart padding and
ordering, deduplication across SIMs/restarts, storage failure, exact upload
acknowledgements, lost acknowledgements, TLS trust, fragmented/invalid RIL
frames, settings validation, native rendering, scanned-network parsing/control
sockets, absent-SIM registration clearing, incoming-call/receipt indicators,
event-log privacy, outgoing/uncertain sends and the narrow Binder permission
policy, including untrusted UIDs and malformed parcels. Call controls are
checked for concurrent SMS reception, explicit hangup indexing, uncertain
results without replay, and rejection of ambiguous/multiple call states.
Three additional boot tests cover staging directory permissions, preservation
of ramdisk payloads and replacement of the invalid legacy init service name.
The runtime tests use synthetic modem sockets and real local HTTP(S) receivers. They
do not establish hardware boot or radio compatibility.

The recovery installer, if produced, targets only boot/system. No build tool
flashes the phone, changes its installed app, or modifies a PC service. The
prepared native firmware must remain separate from the earlier failed audio
experiment and its card packages. Preserve the verified stock restore path
and review the concrete native package before a supervised hardware trial.
First validate boot/ADB, screen/keypad, Wi-Fi/DHCP/DNS/TLS and SIM registration;
then test fresh incoming SMS, each SIM, offline queue recovery and one
owner-initiated outgoing SMS. Check reception after full reboot and screen
blanking. Browser answer/hangup and two-way audio are now verified on SIM 2
through the call relay. A normal phone keypad calling interface, full power
management and long unattended operation remain unimplemented or unproven.

Protocol references: [AOSP Android 6 RIL definitions](https://android.googlesource.com/platform/hardware/ril/+/android-6.0.1_r1/include/telephony/ril.h),
[RIL socket framing/dispatch](https://android.googlesource.com/platform/hardware/ril/+/android-6.0.1_r1/libril/ril.cpp),
[Android 6 netd command definitions](https://android.googlesource.com/platform/system/netd/+/android-6.0.1_r1/server/CommandListener.cpp),
[AOSP permission controller wire protocol](https://android.googlesource.com/platform/frameworks/native/+/android-6.0.1_r1/libs/binder/IPermissionController.cpp),
[AOSP native Binder service registration](https://android.googlesource.com/platform/frameworks/native/+/android-6.0.1_r1/cmds/servicemanager/binder.c).
