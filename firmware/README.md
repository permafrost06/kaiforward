# Boot and system images

This is the native firmware source ported from the PoC for the Nokia 8110 4G
TA-1059. It replaces Gecko with a keypad UI, durable SMS forwarding, Wi-Fi
configuration and a native call relay. Gerda supplies the base system and
ramdisk. The Nokia kernel patch enables VoiceMMode1/2 playback; every kernel
module, including Wi-Fi, is rebuilt to match it.

The source tree contains:

- `native/`: native UI, RIL client, SMS storage/upload and init services.
- `relay/`: TLS call transport and Android PCM/audio routing.
- `kernel/`: playback patch and the stock `3.10.49` configuration captured
  from the PoC's TA-1059 before modification.
- `sources.json`: pinned Gerda, Nokia kernel, WLAN and GCC revisions.
- `scripts/`: PC build, image assembly and validation tools.

Downloads and build work go under `data/`; artifacts go under `dist/`.
Neither directory is tracked. Images contain no Wi-Fi password, receiver token
or device-specific configuration. Settings live in `/data/kaiforward/config`.

## Build

Run the Python tools from the repository root with `python3 -m`, as shown
below. This lets the firmware tools share helpers with the root `scripts/`
package. Settings provisioning lives in `scripts/configure_native.py`.

Use Linux with Python 3.11 or newer (3.12 or newer for automatic archive
extraction), Git, make, GCC, cpio, gzip, e2fsprogs (`debugfs` and `e2fsck`),
and the development headers for the upstream image tools (zlib and libuuid).
An ARM-capable objcopy is also required to strip rebuilt modules.

Prepare the pinned repositories and image tools, then build the kernel/WLAN:

```sh
python3 -m firmware.scripts.prepare_sources
sh firmware/scripts/build_kernel.sh
```

The pinned Android GCC 4.8 compiler runs on x86_64 Linux. On an aarch64 host,
run the kernel build in an x86_64 compiler environment; the PoC used muvm/FEX:

```sh
python3 -m firmware.scripts.run_in_pty muvm --interactive --tty \
  --gpu-mode=software --fex-image=/usr/share/fex-emu/RootFS/default.erofs \
  --emu=fex /bin/sh "$PWD/firmware/scripts/build_kernel.sh"
```

`GATEWAY_BUILD_ROOT` changes the kernel build directory; `GATEWAY_JOBS` changes
parallelism. Pass the corresponding paths to subsequent commands if changed.
The saved stock configuration can be replaced with `--stock-config` during
source preparation.

Build both static ARM helpers and stage the kernel/modules:

```sh
python3 -m firmware.scripts.build_native
python3 -m firmware.scripts.build_call_relay
python3 -m firmware.scripts.prepare_call_kernel \
  --objcopy /path/to/arm-objcopy
python3 -m firmware.scripts.build_native_image --output dist/native-images
```

The automatic Zig 0.15.2 download is for aarch64 Linux. Other hosts can pass
`--zig /path/to/zig --sqlite /path/to/sqlite-amalgamation-3500400` to both
helper builders. Zig/SQLite and Mbed TLS 3.6.7 archives are checksum-verified.
The call helper reuses the Zig/SQLite paths prepared by the native builder.

Kernel staging checks source revisions, the patch, module release and symbol
CRCs, and preserves Gerda's 46 appended device trees. It needs no previously
built boot image or saved modules. The image builder creates a fresh native
ramdisk with traversable mount directories and short legacy init service
names. It disables `b2g`, `rilproxy` and `api-daemon`, installs both helpers and
the matching modules, then checks boot layout, payload hashes, executable
modes and a clean ext4 filesystem.

Outputs include `boot.img` (26,165,248 bytes), `system.img` (838,860,800 bytes),
`manifest.json`, `validation.json` and `SHA256SUMS`. Output directories must be
empty. A public CA bundle is copied from the build host; use `--ca-bundle` to
select a specific bundle. Recheck an existing build with:

```sh
python3 -m firmware.scripts.build_native_image \
  --output dist/native-images --validate-only
```

## Package and configure

Use the repository's flasher to create install and restore packages:

```sh
python3 -m scripts.flasher native \
  --system dist/native-images/system.img --boot dist/native-images/boot.img \
  --restore-from backups/<stock-backup>
```

See the root README for copying packages and installing through recovery.
Build commands only produce PC files. Hardware validation remains separate;
the PoC demonstrated native startup, SMS forwarding and two-way browser call
audio on SIM 2, but this port's newly assembled images need their own trial.

The SMS service uploads to the `/v1/messages` receiver protocol, implemented
by the Convex receiver in [`receiver/`](../receiver/README.md). The call
helper connects separately to `relay_url` (`tls://host[:port]`, port 8769 by
default), served by [`call-relay/`](../call-relay/README.md). Both
authenticate with the receiver token. The helper verifies the relay with
`/data/kaiforward/relay-ca.pem` if present, else `ca.pem`, else the bundled
roots. It reconnects when `relay_url` or `token` changes. An empty
`relay_url` disables it.

Prepare private settings with:

```sh
python3 -m scripts.configure_native \
  --ssid 'Your Wi-Fi network' \
  --receiver-url 'https://receiver.example/v1/messages' \
  --receiver-config /path/to/private-receiver-config.json \
  --ca /path/to/receiver-ca.pem \
  --relay-url 'tls://100.64.0.1:8769' --relay-ca /path/to/relay.crt
```

The receiver config contains `token`. Passwords are prompted
without echoing. Add `--install --serial <serial>` to provision an installed
native phone through root ADB. This writes settings and requests a reload.
The keypad UI can edit Wi-Fi and destination settings; certificates are
provisioned from the PC.

### Automatic Wi-Fi time synchronization

The Wi-Fi supervisor starts BusyBox NTP after Wi-Fi has a default route and
keeps it running to correct clock drift. It stops the client when Wi-Fi goes
away and restarts it after reconnects or client failures. This restores the
clock automatically after battery removal, allowing HTTPS uploads and Tailscale
to recover. A clock correction requests an immediate SMS upload retry.

The client uses Cloudflare's [published NTP addresses](https://developers.cloudflare.com/time-services/ntp/usage/),
`162.159.200.1` and `162.159.200.123`, so startup does not depend on DNS or TLS.
The Wi-Fi network must allow outbound UDP port 123. Diagnostics are in
`/data/kaiforward/ntp.log`; the phone's time can be checked with `adb shell date -u`.
Install this change on an existing native phone with
`python3 -m scripts.update_native --build`. No reboot or firmware flashing is needed.

## Settings API

Use `python3 -m scripts.settings_native get` to read redacted phone settings,
`python3 -m scripts.settings_native set settings.json` to apply a partial JSON
object, and `python3 -m scripts.settings_native key` to retrieve the HTTP access
key. These commands require root ADB. Pass `--serial <serial>` before the
subcommand to choose a device. The setter stages a private file on the phone
and removes it after the command, including on failure.

The native executable also exposes these commands directly:

```sh
adb shell /system/bin/kaiforward-native --settings
adb push settings.json /data/kaiforward/settings-input.json
adb shell 'chmod 600 /data/kaiforward/settings-input.json && /system/bin/kaiforward-native --settings-set < /data/kaiforward/settings-input.json'
adb shell rm /data/kaiforward/settings-input.json
adb shell /system/bin/kaiforward-native --settings-key
```

`--settings-set` reads JSON from stdin. Supported fields are `ssid`, `password`,
`url`, `token`, `relay_url` and `app_token`, all strings. Omitted fields retain their current values.
Configs written by earlier builds may contain a `device_id` line; it is ignored
and dropped on the next save. Settings may be provisioned in stages: an empty SSID clears the saved network,
an empty password selects an open network, an empty URL or token disables
uploading, and an empty `relay_url` disables the call relay. Invalid fields, duplicate keys, invalid credentials, and control
characters reject the entire patch. The maximum JSON body is 16384 bytes.
Settings are saved atomically with mode 0600, followed by `reload` and `sync`
requests. Keypad pages refresh external changes while no field is being edited.

The existing SMS service hosts HTTP on `0.0.0.0:8768`, so ordinary runtime
updates enable it without new init services. `--settings-bind IPv4` and
`--settings-port PORT` override the listener when launching the daemon manually.
If the listener cannot start, SMS and call handling continue. Nonblocking
clients have bounded buffers and five-second deadlines.

HTTP clients use `Authorization: Bearer <settings-key>` for both
`GET /v1/settings` and `PATCH /v1/settings`. Patches require
`Content-Type: application/json` and a `Content-Length`; chunked bodies are
unsupported. A successful response contains `ssid`, `url`, `relay_url`,
`password_set`, `token_set`, and `app_token_set`, with no passwords, receiver tokens, or access
keys. Invalid requests return 400, missing/wrong keys 401, unknown paths 404,
unsupported methods 405, a busy settings writer 409, oversized requests 413,
wrong content types 415, and storage failures 500. Responses disable caching.

The access key is generated from `/dev/urandom`, saved privately in
`/data/kaiforward/settings.key`, and retained across reboot and runtime updates.
It is independent of the receiver token. To rotate it, stop `kaiforward-sms`,
remove `settings.key` over root ADB, and start the service; retrieve the new key
with `scripts.settings_native key`.

`GET /v1/status` uses the same key and returns live phone state as JSON;
other methods return 405. Values the phone cannot read are `null`.

- `time` (Unix ms) and `uptime` (seconds).
- `battery`: `level` (percent), `status` (`charging`, `discharging`, `full`,
  `not charging`), `charging`, `plugged` (`usb`, `ac`, or `null`),
  `temperature` (°C), and `voltage_mv`.
- `wifi`: `connected` (wlan0 carrier), `configured_ssid`, the supplicant's
  `state` (such as `completed` or `scanning`) and associated `ssid`, `ip`,
  `signal_dbm`, `link_mbps`, and `frequency_mhz`. The driver leaves
  `/proc/net/wireless` empty, so `--scan-wifi` saves wpa_supplicant `STATUS`
  and `SIGNAL_POLL` results to `wifi.link` about every 10 seconds; results
  older than a minute are ignored.
- `sims`: one object per slot with `state` (the label shown on screen, such as
  `registered`, `searching`, `absent`, or `disconnected`), `connected` (rild
  socket), `radio_on`, `sim` (`ready`, `pin`, `puk`, `absent`, `error`,
  `initializing`, `unknown`), `registered`, and `call`.
- `call`: `active`, `slot`, `state` (`idle`, `active`, `holding`, `dialing`,
  `alerting`, `incoming`, `waiting`), and `incoming`. Numbers are not exposed.
- `messages`: `pending_upload`, `received`, `sent`, `undecoded` raw PDUs,
  `incomplete` multipart groups, and `outbox` counts by status (`queued`,
  `submitting`, `sent`, `failed`, `uncertain`).
- `upload`: `configured` (URL and token set), `state` (`idle`, `syncing`,
  `retrying`), and `updated`, the uploader's last status time in Unix ms.

See the [ADB and curl examples](../README.md#change-settings-over-adb-or-http).
HTTP has no TLS, so use an ADB forward or a trusted LAN. A Wi-Fi change can
disconnect a LAN client before it receives the response; read settings after
reconnecting. CA bundles remain provisioned with `scripts.configure_native`.

## Update over ADB

Once native firmware is installed, ordinary userspace edits do not need
boot/system images. `scripts.update_native` uses Gerda root ADB to replace
the native executable, Wi-Fi/DHCP scripts and font license. It optionally
replaces the call relay too. The existing init services still launch the same
paths, so updates survive reboot without changing startup definitions.

Check compatibility without writing to the phone, then build and push:

```sh
python3 -m scripts.update_native --check
python3 -m scripts.update_native --build
```

`--build` downloads and checksum-verifies the pinned Zig/SQLite inputs in
this project's `data/native-build/`, then reuses those archives on later
builds. `--build-root` optionally selects a different cache directory;
the default build does not use the old `poc/` directory. Pass `--zig` and
`--sqlite`, as with `firmware.scripts.build_native`, to use your own toolchain.
To install a runtime already built:

```sh
python3 -m scripts.update_native --runtime dist/native-runtime
```

To update the call helper, build it with `firmware.scripts.build_call_relay`,
then add `--relay-runtime dist/native-call-relay` to the updater. `--build`
builds only the native runtime. Use `--serial <serial>` to select a phone.

Before replacing anything, the updater rejects stale sources, damaged
payloads, non-ARM binaries and mismatched init definitions. It stages and
hash-checks the bundle in `/data/kaiforward/updates/<update-id>/`, runs a small
ARM execution check, and backs up the installed files. It stops native
services, copies each replacement to a sibling temporary file, renames it
into place, checks installed hashes and restarts the previously running
services. It restores `/system` to read-only if it started that way.
Settings, certificates and the SMS database are preserved. Updates briefly
interrupt native services, Wi-Fi and call handling; run them while idle.

An ordinary install error or termination signal triggers restoration of the
previous runtime. Backups remain on the phone, and the updater prints the
command to undo a successful update:

```sh
python3 -m scripts.update_native --rollback <update-id>
```

If the connection drops, reconnect and inspect the services with `--check`
before retrying. A power cut or forced kill can interrupt the multi-file
transaction. After reboot, if a stale `/data/kaiforward/update.lock` remains
and no updater is running, remove that empty directory with root ADB, then
run `--rollback <update-id>` using the ID under the phone's `updates/`
directory. Keep the backup directory until the update has been tried on
hardware; a startup check only proves that the processes remain running.

Kernel/Wi-Fi-module changes and edits to ramdisk init definitions still use
the image build and recovery-flash workflow. Receiver settings use
`scripts.configure_native`, `scripts.settings_native`, the HTTP API, or the
keypad UI, independently of code updates.

## Tailscale startup

Use the PC installer with the phone connected through root ADB and Wi-Fi:

```sh
python3 -m scripts.tailscale install
```

It downloads the tested static ARM version pinned in
`native/tailscale-inputs.json`, verifies its SHA-256 and architecture, supplies
a public CA bundle, and installs the runtime and startup script. Existing login
state stays on the phone. For a new node, open the printed login link and then
run `python3 -m scripts.tailscale status` to confirm it joined your tailnet.

On older native installations, it backs up the actual boot partition on both
the phone and PC, appends only the Tailscale service to the existing native init
file, and checks every other ramdisk file plus the kernel/DTBs remain unchanged.
It rejects unsupported layouts and trailing signatures. Boot writes are verified
by readback. When the service is already present, its boot image is unchanged;
repeating an identical installation skips payload replacement too.

The installer never reboots. It starts the supervisor manually when init has
not loaded the service yet. Restart the phone yourself to activate a newly
added init service. Boot/kernel changes from other work must not be rolled back
using an older Tailscale installation ID; the rollback command rejects that.

Management commands work before and after the first reboot:

```sh
python3 -m scripts.tailscale status
python3 -m scripts.tailscale login
python3 -m scripts.tailscale stop
python3 -m scripts.tailscale start
python3 -m scripts.tailscale rollback <installation-id>
```

`stop` stops the current session while retaining login and boot startup. Rollback
restores the preceding binaries, script and boot image while retaining login
state. Ordinary installation/rollback failures run recovery on the phone;
a power cut or forced kill can still interrupt the transaction. Its saved bundles
remain under `/data/kaiforward/tailscale-installs/`, and verified PC boot backups
live under `backups/tailscale/`. A stale installation lock after such an
interruption needs inspection before removal.

Use `--serial <serial>` before the subcommand to select a phone. To reuse an
already downloaded archive or supply a public CA bundle:

```sh
python3 -m scripts.tailscale --serial <serial> install \
  --archive /path/to/tailscale_1.104.1_arm.tgz \
  --ca-bundle /path/to/public-ca-bundle.pem
```

The local archive must match the pinned digest. `--cache <directory>` changes
the download/extraction cache from `data/tailscale-install/`.

The `kf-tailscale` init service runs `/system/kaiforward/tailscale.sh`. Provision
the static ARM `tailscale` and `tailscaled` binaries and a public
`ca-certificates.crt` bundle in `/data/kaiforward/tailscale/`, with that directory
mode 0700 and the binaries executable. The firmware images include the startup
script; the separately provisioned binaries and authenticated `state/` directory
remain on `/data` across boot/system image updates.

The service waits for Android's Wi-Fi default route, creates the TUN device link,
and maintains the routing rule required by Tailscale's marked outer packets.
It restarts the daemon if it exits and restores the rule if netd removes it.
Authenticated state retains the running preference, so boot needs no new login.
The current integration uses Wi-Fi, leaves Android DNS unchanged, and does not
select an exit node or accept subnet routes.

After boot, inspect or stop the service through root ADB:

```sh
adb shell '/data/kaiforward/tailscale/tailscale --socket=/data/kaiforward/tailscale/tailscaled.sock status'
adb shell stop kf-tailscale
adb shell start kf-tailscale
```

Adding the init service to an older installation requires a boot image update;
editing the script thereafter is supported by the native runtime updater.

## Checks and licenses

```sh
python3 -m unittest discover -s tests -v
```

Host tests need GCC, SQLite development headers, curl and OpenSSL. They use
synthetic modem sockets and a receiver fixture under `tests/support/`, with
no phone connection. They cover SMS decoding/durability/retries/TLS, call
control, Wi-Fi configuration, settings commands/HTTP authentication and updates,
Binder permissions, startup and kernel session
selection. Update tests run the install/rollback shell transaction against a
disposable host filesystem, including transfer corruption, partial-copy
failures, termination signals and startup crashes. They do not prove carrier
or hardware behavior.

The embedded font retains its license in `native/FONT-LICENSE.txt`. SQLite is
public domain and Mbed TLS is Apache-2.0 OR GPL-2.0-or-later. The Linux kernel
and its integrated playback patch are GPL-2.0-only; Gerda's system includes
third-party components with their respective upstream licenses. Downloaded
sources retain their license files. These component licenses also apply when
distributing firmware images; the root MIT license covers KaiForward's own
userspace code.

## SSH access

Build and install the pinned [Dropbear](https://github.com/mkj/dropbear) server
in one command:

```sh
python3 -m scripts.ssh install --public-key /path/to/key.pub
ssh root@<phone-wifi-or-tailscale-ip>
```

The installer requires root ADB and an Ed25519 public key supplied explicitly
with `--public-key /path/to/key.pub`. It never selects a key automatically.
It downloads verified build inputs, builds the static ARM server on the PC,
and reuses valid builds from `data/ssh-install/`. Add `--cache <directory>` to
change that cache, `--zig /path/to/zig` to use your own compiler, or
`--runtime dist/ssh-runtime` to install a runtime built separately with
`python3 -m firmware.scripts.build_ssh`. Linux, Python 3.11+, make and
`ssh-keygen` are required; Zig is prepared automatically if needed.

It verifies the runtime and all transfers, saves a verified boot backup, and
adds `kf-ssh` without changing the kernel or other startup services, and prints a guarded rollback
command. SSH starts immediately; restart the phone yourself when ready to load
the new init service. The installer never reboots. Repeating an identical
install skips payload and boot writes, retains the enabled/disabled setting,
and retains host keys.

Only the `root` account exists. Password authentication is compiled out.
Host keys, authorized keys and binaries live in `/data/kaiforward/ssh` and
survive firmware updates. Installation replaces `authorized_keys` with the
supplied key. Port 22 listens on Wi-Fi and Tailscale; TCP forwarding is disabled.
Shell commands and interactive terminals work; this build has no SFTP server.
Use ADB for file transfers. The upstream license is installed beside the server.

```sh
python3 -m scripts.ssh status
python3 -m scripts.ssh stop
python3 -m scripts.ssh start
python3 -m scripts.ssh rollback <installation-id>
```

`stop` persists across reboot; `start` clears that setting. Rollback restores
the previous payload and boot image, refusing to overwrite later boot changes.
It retains generated host keys. Add `--serial <serial>` before the subcommand
when multiple phones are connected. For SSH over USB:

```sh
adb forward tcp:2222 tcp:22
ssh -p 2222 root@127.0.0.1
```

## Direct SMS API

The Nokia accepts authenticated SMS requests on the existing native HTTP
listener (port 8768). Calls keep their outgoing TLS connection to `relay_url`;
SMS requests do not depend on that connection.

Set `app_token` using the settings API or `scripts.settings_native set` with a
private JSON file containing `{"app_token":"<your app token>"}`. Use the same
value as the receiver's `KAIFORWARD_READ_TOKEN`. Do not change the Nokia's upload
`token`. Reads report only `app_token_set`; the token is not returned. Empty
`app_token` disables direct SMS access. Changes take effect on the next request.
Only the settings access key can change settings; the app token grants SMS
access. The settings key and upload token do not authorize the SMS endpoints.

Rebuild and update both the native runtime and call helper: both read the
configuration format containing `app_token`.

```sh
python3 -m firmware.scripts.build_call_relay
python3 -m scripts.update_native --build --relay-runtime dist/native-call-relay
```

After updating, enable Tailscale Serve **on the Nokia**:

```sh
adb shell /data/kaiforward/tailscale/tailscale --socket=/data/kaiforward/tailscale/tailscaled.sock serve status --json
adb shell /data/kaiforward/tailscale/tailscale --socket=/data/kaiforward/tailscale/tailscaled.sock serve --bg 8768
```

Inspect existing Serve configuration before changing it; if HTTPS port 443
already serves another app, use a separate port, such as `serve --bg
--https=8443 8768`, and include `:8443` in the Android URL. Tailscale HTTPS
certificates must be enabled for the tailnet. Serve terminates HTTPS on the
Nokia and proxies to its own loopback listener. Enter the HTTPS origin printed
by Serve in the Android app; no separate machine hosts this SMS endpoint.
See [Tailscale Serve documentation](https://tailscale.com/docs/reference/tailscale-cli/serve).

Every endpoint requires `Authorization: Bearer <app_token>` and disables caching:

- `GET /v1/sms` verifies access and returns `{"slots":[1,2]}`. Both SIMs can queue
  SMS even when not registered; transmission waits until the chosen SIM is ready.
- `POST /v1/sms/send` accepts JSON with exactly `requestId` (a lowercase UUID),
  `slot` (1 or 2), `number`, and `text`. It returns 202 with the outbox `job`,
  `slot`, and `status`. It validates the same recipient and one-segment limits
  as `--send`. Content-Type must be application/json with a Content-Length.
- `GET /v1/sms/requests/<requestId>` returns the job, SIM and latest status,
  without recipients or message contents. Unknown IDs return 404.

Request IDs and queued jobs commit atomically to SQLite. Repeating a request
with the same ID and payload returns the same job, including after a service
restart. Reusing an ID with a different payload returns 409. Clients should
save the ID before submitting and recover lost responses with GET. Do not
create another ID automatically after a timeout. A queued job is not a delivery
receipt; the existing radio path sends once and marks interrupted submissions
uncertain. Invalid payloads return 400, invalid credentials 401, disabled SMS
access 503, and storage failures 500. Existing settings/status access continues
to require the settings key.

While an SMS is queued, the radio worker refreshes stale registration state
at most every 15 seconds. Once submitted, it waits up to three minutes for the
modem result. A disconnect or expired result remains uncertain and is never
automatically resent; the recipient may already have received it.
