# KaiForward

## Build boot and system images

The native firmware source is in [`firmware/`](firmware/README.md): keypad UI,
SMS forwarding, Wi-Fi, call relay and the patched Nokia kernel. Its build tools
produce boot/system images on the PC for packaging with `scripts.flasher`.
See the [firmware build instructions](firmware/README.md) for dependencies,
pinned sources, validation and provisioning.

## Update native code over ADB

An installed native Gerda phone can receive UI, SMS, Wi-Fi and call-helper
updates over USB. Check the connected phone without changing it:

```sh
python3 -m scripts.update_native --check
```

Build and install the native runtime in one command:

```sh
python3 -m scripts.update_native --build
```

The builder downloads and verifies Zig/SQLite in this project's
`data/native-build/`, and uses that cache on later builds. The build and
update commands are independent of the old `poc/` directory.

Use `--zig /path/to/zig --sqlite /path/to/sqlite-amalgamation-3500400` for
existing build inputs. To push a runtime already built, use
`--runtime dist/native-runtime`. Add `--serial <serial>` when multiple phones
are connected, or `--relay-runtime dist/native-call-relay` to include a rebuilt
call helper.

The updater verifies build and transfer hashes, saves the previous files,
replaces the native runtime on `/system`, and restarts the native services.
Updates persist across reboot; settings and stored SMS remain in `/data`.
Services briefly stop, including Wi-Fi and call handling. Each successful
update prints a rollback command. See the [firmware instructions](firmware/README.md#update-over-adb)
for the recovery procedure and which changes still need images.

## Change settings over ADB or HTTP

After installing the updated native runtime, read settings or apply a partial
JSON file without prompts:

```sh
python3 -m scripts.settings_native get
python3 -m scripts.settings_native set settings.json
```

For example, `settings.json` can contain:

```json
{"ssid":"Your Wi-Fi","password":"your-password"}
```

The writable fields are `ssid`, `password`, `url`, `token` and `relay_url`.
`url` is the SMS receiver (`https://…/v1/messages`); `relay_url` is the
[call relay server](call-relay/README.md) (`tls://host[:port]`).
Omitted fields keep their current values. Empty `password` selects an open
network; empty `url` or `token` disables forwarding, and empty `relay_url`
disables the call relay. Reads and update responses
hide credentials and report `password_set` and `token_set` instead. Add
`--serial <serial>` before `get`, `set`, or `key` to select a phone.

The phone also serves `GET /v1/settings` and `PATCH /v1/settings` on port 8768.
Both require a separate settings access key. Retrieve it over root ADB, then
forward the HTTP port to your PC:

```sh
umask 077
python3 -m scripts.settings_native key | sed 's/^/Authorization: Bearer /' > settings-http.header
adb forward tcp:8768 tcp:8768
curl -H @settings-http.header http://127.0.0.1:8768/v1/settings
curl -X PATCH -H @settings-http.header -H 'Content-Type: application/json' \
  --data-binary @settings.json http://127.0.0.1:8768/v1/settings
```

For direct Wi-Fi access, replace `127.0.0.1` with the phone's IP. HTTP is
unencrypted; use the USB forward or a trusted network. Changes persist across
reboot and request Wi-Fi reload and upload sync automatically. Changing Wi-Fi
can disconnect a direct HTTP session. Initial HTTPS receiver certificates
still use `scripts.configure_native --ca`; see the
[firmware settings reference](firmware/README.md#settings-api).

## Call relay server

Browser call control runs on a separate server reachable over Tailscale, not
on Convex. On that server, with the phone on root ADB, prepare its private
token and certificate and point the phone at it:

```sh
python3 -m scripts.call_relay
```

See [`call-relay/`](call-relay/README.md) for running the server.

## Install Tailscale

On native KaiForward firmware with root ADB and working Wi-Fi, run:

```sh
python3 -m scripts.tailscale install
```

This downloads and verifies the tested ARM binaries, preserves saved login
state, installs the startup supervisor, and adds the boot service if needed.
It saves verified boot backups and prints a rollback command. **It never
reboots the phone.** If a boot change was needed, restart the phone yourself
when ready. New installations print a Tailscale login link to open on your PC.

```sh
python3 -m scripts.tailscale status
python3 -m scripts.tailscale login
python3 -m scripts.tailscale stop
python3 -m scripts.tailscale start
```

Use `--serial <serial>` before the subcommand when several phones are connected.
See [the firmware instructions](firmware/README.md#tailscale-startup) for offline
installation, rollback and the Wi-Fi routing integration.

## SSH on custom firmware

Install key-only root SSH with persistent host keys and boot startup:

```sh
python3 -m scripts.ssh install --public-key /path/to/key.pub
ssh root@<phone-ip>
```

Use the phone's Wi-Fi or Tailscale address. The installer saves a verified boot
backup and prints rollback instructions; it never reboots. See the
[SSH reference](firmware/README.md#ssh-access) for USB access and management.

## Root access

Reading partitions needs root on the phone. Custom firmware such as the Gerda
build runs `adbd` as root. Stock firmware does not, so start the temporary root
shell first:

```sh
python3 -m scripts.access
```

Then open **http://127.0.0.1:8767** in the phone's browser and press
**Enable temporary access**. The command returns once the shell answers.

The page (`scripts/access.html`) calls the firmware's `startUniversalCommand`
API, which the browser can reach on this firmware. The root `b2g` system
process runs the command: `busybox telnetd` bound to the phone's loopback
interface. Nothing is installed, and rebooting the phone stops it. The page is
served from the PC through an ADB reverse tunnel; the phone's browser does not
open `file://` addresses. `scripts/root_shell.py` reaches the shell through an
ADB forward from PC port 12323 to phone port 2323.

## Back up the phone

Copy every eMMC partition plus both GPT copies to the PC, or only the named
partitions:

```sh
python3 -m scripts.backup                 # everything, into backups/<serial>-<timestamp>/
python3 -m scripts.backup boot modem      # only these partitions
python3 -m scripts.backup --list          # partitions available to back up
python3 -m scripts.backup --resume backups/<dir>
```

The backup uses root `adbd` when available, otherwise the temporary root
shell (`--transport` forces either). Through the root shell, the PC listens on
its loopback interface and the phone sends each image to it through an ADB
reverse tunnel, so the phone opens no listening port.

Nothing on the phone is written except a temporary directory in `/dev`
(tmpfs), which is removed afterwards. Each region is read once on the phone;
`tee` feeds the same bytes to the USB stream and to a phone-side SHA-256, and
the PC accepts an image only when that hash and the expected size match its
own. Progress is saved in `manifest.json` after every image, and `--resume`
re-hashes saved images before continuing. A full run ends by checking that
both GPT copies are intact and describe the kernel's partition layout.

Partitions mounted read-write while the phone runs (`userdata`, `cache` and
`persist` on stock firmware) are copied live and marked `live` in the manifest.
Each live ext4 image is checked by replaying its journal and running
`e2fsck -fy` on a disposable reflink copy; the stored image is never modified.
Stale free-space counters are expected in live copies, and this phone's
formatter leaves the inode bitmap padding unset; both are treated as harmless.
The RPMB area cannot be read and is not included.

## Flash firmware from recovery

`scripts.flasher` builds two small signed packages for the phone's stock
recovery: one flashes the given images, the other restores stock firmware to the
same partitions. Each writes `system`, then `boot`, from images kept on the
microSD card:

```sh
python3 -m scripts.flasher <name> --system <system.img> --boot <boot.img> \
    --restore-from backups/<dir>
```

Give `--system`, `--boot` or both. `--restore-from` is a directory with the
stock `system.img`/`boot.img`, such as a backup. The results go into
`dist/<name>/`, laid out as they go in the card's `kaiforward/` folder:

- `<name>.zip` and `<name>/`, which flash the given images,
- `<name>-restore.zip` and `<name>-restore/`, which restore stock.

Copy the contents of `dist/<name>/` into a `kaiforward` folder on the card, or
add `--push` to copy them over ADB and check their hashes on the phone.
`--push` creates `kaiforward/` if needed. If the card is not mounted, as on
custom firmware, it mounts the card on a temporary directory (this needs root
`adbd`) and unmounts it afterwards.

To install, power off the phone and hold **D-pad Up** while pressing **Power**
to enter recovery. Choose **Apply update from SD card**, then
`kaiforward/<name>.zip`. When it finishes, choose **Reboot system now**. To go
back to stock, install `kaiforward/<name>-restore.zip` the same way. Recovery
writes a log to the package's image folder on the card, such as
`kaiforward/<name>/flash.log`.

The package refuses to write anything unless all of these hold:

- every image matches the hash and size recorded in the package,
- every target partition has the same size as its image and is not mounted.

After writing each partition, the package drops the page cache and checks the
partition's readback hash. It never writes recovery, radio, bootloader or data
partitions, and does not wipe data.

Stock recovery only accepts packages signed with the AOSP test key. The
recovery updater, busybox and test key are in `scripts/recovery/`, which
records where each comes from, its license and its source; the flasher does
not run without them. Signing uses `openssl`, and its output is byte-identical
to AOSP `signapk -w`, so Java is not needed.

## License

KaiForward is licensed under the MIT License; see `LICENSE`. The third-party
files in `scripts/recovery/` keep their own licenses, described in
`scripts/recovery/README.md`. Firmware components retain the licenses described
in [`firmware/README.md`](firmware/README.md), including the GPL kernel patch
and the embedded font license.
