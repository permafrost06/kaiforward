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
by the Convex receiver in [`receiver/`](../receiver/README.md). The browser
call relay is not included in this firmware port. The call helper connects to
the same configured host over TLS on port 8769 by default. Both authenticate
with the receiver token alone; the relay expects the PoC call protocol.

Prepare private settings with:

```sh
python3 -m scripts.configure_native \
  --ssid 'Your Wi-Fi network' \
  --receiver-url 'https://receiver.example/v1/messages' \
  --receiver-config /path/to/private-receiver-config.json \
  --ca /path/to/receiver-ca.pem
```

The receiver config contains `token`. Passwords are prompted
without echoing. Add `--install --serial <serial>` to provision an installed
native phone through root ADB. This writes settings and requests a reload.
The keypad UI can edit Wi-Fi and destination settings; certificates are
provisioned from the PC.

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
`url` and `token`, all strings. Omitted fields retain their current values.
Configs written by earlier builds may contain a `device_id` line; it is ignored
and dropped on the next save. Settings may be provisioned in stages: an empty SSID clears the saved network,
an empty password selects an open network, and an empty URL or token disables
uploading. Invalid fields, duplicate keys, invalid credentials, and control
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
unsupported. A successful response contains `ssid`, `url`,
`password_set`, and `token_set`, with no passwords, receiver tokens, or access
keys. Invalid requests return 400, missing/wrong keys 401, unknown paths 404,
unsupported methods 405, a busy settings writer 409, oversized requests 413,
wrong content types 415, and storage failures 500. Responses disable caching.

The access key is generated from `/dev/urandom`, saved privately in
`/data/kaiforward/settings.key`, and retained across reboot and runtime updates.
It is independent of the receiver token. To rotate it, stop `kaiforward-sms`,
remove `settings.key` over root ADB, and start the service; retrieve the new key
with `scripts.settings_native key`.

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
