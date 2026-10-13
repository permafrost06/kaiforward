# Experimental Nokia gateway firmware

The current [native UI/SMS variant](NATIVE_UI.md) and [browser call relay](CALL_RELAY.md)
are in `dist/gerda-native-calls-20261013`. The native screen, Wi-Fi forwarding
and call controls run without Gecko. The corrected kernel now boots with the
working native startup and matching modules. The owner confirmed two-way
browser call audio over Wi-Fi and browser hangup on SIM 2. No modem or
calibration partition was changed. The remainder of this document records
the earlier audio experiment and recovery history.

This work prepares a custom Gerda boot/system build for this Nokia 8110 4G TA-1059,
serial `22aaa475`, originally on `12.00.17.10`. The owner reports successful
boot/system installation through stock recovery, but the experimental system
repeatedly returned to recovery. After restoration with `05-restore-stock-sd.zip`,
the owner reports correct boot and the original wallpaper on 2026-10-13.
ADB confirms original firmware `12.00.17.10` (incremental `12.00.17.06`), the
original kernel and completed boot. The saved restore log confirms original
system and boot readback hashes. The owner confirms Robi detected and a normal
call with two-way audio; Teletalk was intentionally removed. The experimental
image is not considered bootable on this hardware.
The call-audio correction has passed a C fixture test but has not been tested
in a booted phone. This is an audio experiment and local health bootstrap;
remote call/SMS/USSD controls and a WebRTC bridge remain later stages.

The current installer is `dist/gateway-experimental/install-boot-system-v2.zip`
(252,483,975 bytes). Its signed payload, partition scope, boot layout, original
46 device-tree blobs, 26 rebuilt modules, executable permissions and clean
ext4 filesystem have been validated. Reports and hashes are beside it in
`validation.json`, `manifest.json` and `SHA256SUMS`. Recovery ZIP signatures,
tamper rejection and mounted-filesystem guards have also been checked.
The inserted microSD card and PC both hold the verified offline backup.

The owner subsequently authorized clearing the 32 GB Transcend card in the PC
reader. It now has one MBR/FAT32 partition labeled `KAIFORWARD`, about 29.35 GiB
free, and the four packages listed below. Its filesystem check and package
hashes after remount passed; it was safely ejected. Preparation details are
recorded in `data/card-preparation.json`. Card preparation did not run a phone
backup or install firmware.

Card filenames are `01-backup-userdata.zip`, `02-install-gateway.zip`,
`03-restore-stock.zip`, and `04-restore-userdata.zip`, corresponding to the
package descriptions below. `README.txt` and `SHA256SUMS` are also on the card.

The phone recognized the inserted card, all four package hashes matched, and
the battery was full. The first stock-recovery backup attempt stopped with
Status 7 at the live `getprop("ro.product.device")` assertion, before mounting
or copying anything. The legacy updater did not return the expected live
property; the exact returned value was not logged. Version 2 packages retain
the device check using `file_getprop("/default.prop", "ro.product.device")`:
this phone's verified recovery property file contains `Nokia 8110 4G`.
The backup script now logs its phases and more specific guard failures.
All four version 2 packages have now replaced the original ZIPs on the phone's
card, with verified hashes and an updated `SHA256SUMS`. A read-only test using
the actual updater binary on the live phone confirmed that its old lookup
returns an empty device name, while parsing the saved recovery property file
returns `Nokia 8110 4G` and passes the new assertion. Version 2 then completed
the offline backup in stock recovery. Original version 1 ZIPs on the PC are
superseded, including the restore packages with the failing guard.
The owner subsequently authorized installation and observed the final
installer message followed by `Install from SD card complete` in stock
recovery. Subsequent reboot attempts repeatedly returned to recovery; normal
boot and partition/kernel verification have not succeeded.

The offline userdata/cache images are copied to
`backups/TA-1059-offline-userdata-20261012-232909` on the PC and remain on the
card. Their SHA-256 hashes match the recovery checksum file and their exact
sizes match the phone partitions. Modern read-only filesystem checks flag
only unused inode-bitmap padding in both original images. Disposable reflink
copies pass clean checks after correcting that padding; original backup
images retain their exact hashes and have not been modified. Check reports
and a manifest are alongside the images. The owner confirmed that button
entry into recovery works from power-off without ADB. The original and fresh
firmware copies of boot/system/recovery also have identical hashes.

`data/recovery-readiness.json` records the combined backup and installation
readiness. The owner explicitly authorized boot/system installation after the
backup checks. Final preflight confirmed the installer hash on the phone,
full battery and no active voice PCM; stock recovery entry was requested for
installation. The owner reports installation completed, followed by repeated
returns to recovery. Selecting `03-restore-stock.zip` aborted without a visible
specific error. A fresh ADB sideload attempt with the hash-verified stock ZIP
returned `Total xfer: 0.00x` and disconnected. This does not confirm any restore
operation. The later card-image restoration below restored owner-reported
normal boot; the experimental boot failure remains unexplained.
Userdata/cache restoration has not been attempted.

Read-only diagnostic collection in a fresh recovery session saved logs to the
card; they are copied to `data/gateway-build/recovery-logs-20261013`. Earlier SD
and ADB restore aborts show battery capacity `0` with status `Full`, followed
by abort before the package is opened. After battery removal, recovery reports
`92`/`Charging` and executes the diagnostic. Original recovery partition hash
matches its backup; boot partition hash matches the experimental image.
The diagnostic v1 log collection succeeded but its final `ui_print(read_file())`
failed because `read_file` returns a blob. That display step is removed in v2.

A fallback small installer is `restore-stock-from-microsd-v1.zip`, staged as
`05-restore-stock-sd.zip`; original images go in `kaiforward-stock-restore` on
the card. It embeds hashes from the original verified backup, verifies both
source images and target sizes before any writes, refuses mounted targets
including aliases, then restores and checks system before restoring and
checking boot. It writes no userdata/cache/radio/recovery partition. Fault
injection tests use fake tools without real block devices and cover validation
and write/readback failures. After this fallback, the owner reports normal
boot and the old wallpaper. ADB subsequently confirmed the original firmware,
kernel and boot completion. The imported `stock-restore-20261013.log` confirms
both original target readback hashes; live unprivileged ADB cannot independently
hash raw targets. `data/gateway-build/stock-restore-result-20261013.json` records
the evidence. The owner confirms normal two-way Robi call audio.

The post-restore compatibility review in
`data/gateway-build/compatibility-review-20261013/report.json` finds matching
stock/experimental boot header addresses and appended device trees. Kernel
configuration differs only in release metadata. This does not identify the
failed normal-boot cause: kernel, ramdisk and system changed together. A future
trial should isolate the smallest audio change against working stock
ramdisk/userspace and capture failed normal-boot evidence if needed.

## What changes

- `voc_start_playback` handles VoiceMMode1 on playback port `0x8005` and
  VoiceMMode2 on `0x8002`, alongside the legacy sessions. It retains locking
  and handover state, prevents reference-count underflow/overflow, propagates
  negative native helper errors, and reports no active session as `ENODEV`.
- Existing native modem-command helpers remain unchanged, including their
  timeout handling. A logged return of zero alone is insufficient evidence
  of successful or audible modem playback.
- A native init service writes a private heartbeat each minute under
  `/data/kaiforward`. It starts automatically after a full boot, does not
  initiate cellular operations, and exposes no network listener.
- The existing bounded call capture/injection probes and test phrase are
  included under `/system/kaiforward`; probe logs persist in `/data/kaiforward`.
- Gerda's original appended device-tree blobs are retained. Loadable modules,
  including Wi-Fi, are rebuilt for the new kernel and checked against its
  release and exported symbol CRCs. The unavailable `core_ctl.ko` is omitted;
  Gerda's post-boot script already excludes this module for Nokia 8110.

The old Gerda filesystem builder leaves invalid padding bits in the last
inode bitmap. Assembly runs `e2fsck` on the new PC image to correct those bits,
then requires a clean read-only check before recording the image hashes.
Android's default file metadata also overrides input chmod outside `bin`;
assembly explicitly preserves execute permission for B2G, its plugin process,
the API daemon and Picolisp without altering their owners or capabilities.
No phone filesystem or partition is repaired or written during this build.

Gerda's root/ADB and SELinux defaults still apply. Treat USB access as trusted.
No authenticated remote gateway endpoint is provided by this bootstrap.

## Build on this PC

Input repositories are pinned in [sources.json](sources.json). The kernel comes
from an archive of HMD's released Nokia source, not a verified reproduction of
the exact stock kernel binary. The saved live kernel config differs only in
the local release name, automatic version suffix, and two source-tree config
options (removed disabled SSM and added disabled CMA page cache).

Build inputs and logs live under `data/gateway-build`, excluded from source
control. The saved input config is `data/gateway-build/stock-kernel.config`.
Preparing/building images is PC-only; none of these scripts use ADB or deploy.

```sh
python3 firmware/scripts/prepare_sources.py \
  --stock-config data/gateway-build/stock-kernel.config
python3 -m unittest discover -s tests -p test_gateway_kernel.py -v
```

The old kernel needs Android GCC 4.8. Its pinned prebuilt is for x86_64. This
aarch64 Asahi PC uses muvm/FEX with a PTY; modern GCC 12 did not compile the
original ARM `put_user` implementation successfully. The native host tools
use GNU C11. To run the kernel/WLAN build here:

```sh
PULSE_SERVER=unix:/run/user/1000/pulse/native \
python3 firmware/scripts/run_in_pty.py muvm --interactive --tty \
  --gpu-mode=software --fex-image=/usr/share/fex-emu/RootFS/default.erofs \
  --emu=fex /bin/sh /home/frost/work/kaiforward/firmware/scripts/build_kernel.sh
```

On x86_64 Linux, run `sh firmware/scripts/build_kernel.sh` directly. Kernel
and WLAN build logs are `kernel-build-gcc48.log` and `wlan-build-gcc48.log`.
After both builds succeed, assemble into a new, empty output directory:

```sh
python3 firmware/scripts/build_gateway_image.py \
  --gerda data/gateway-build/gerda \
  --kernel-source data/gateway-build/kernel \
  --kernel-build data/gateway-build/kernel-out-gcc48 \
  --wlan-module data/gateway-build/kernel/drivers/net/wireless/prima/wlan.ko \
  --output dist/gateway-experimental
```

The default ARM inspection/stripping tools use the Zephyr SDK installed on
this PC. On another machine, set `GATEWAY_NM` and `GATEWAY_OBJCOPY` to the
corresponding ARM tools. Module symbol versions are checked before and after
debug information is stripped; stripping reduces image size.

```sh
python3 firmware/scripts/validate_images.py \
  --images dist/gateway-experimental --gerda data/gateway-build/gerda \
  --kernel-build data/gateway-build/kernel-out-gcc48
```

Signing requires Java 8 for the upstream signing tool. The downloaded Temurin
JRE 8 is checksum-verified against its publisher's metadata; it is kept under
`data/gateway-build/jre8`. Set `GATEWAY_JAVA` to its `bin/java`, then:

```sh
python3 firmware/scripts/package_recovery.py \
  --gerda data/gateway-build/gerda --images dist/gateway-experimental \
  --output dist/gateway-experimental/install-boot-system-v2.zip
```

Whole-file signature checks use `verify_recovery_signature.py`. On Fedora,
`OPENSSL_ENABLE_SHA1_SIGNATURES=1` is needed for this legacy **build validation**;
the system crypto policy is not changed. The expected test certificate's RSA
key matches a trusted key extracted from this phone's stock recovery image.

## Installation gates and sequence

The owner has a microSD card. The card must be FAT32, with at least 4 GiB free
for offline userdata/cache backup. Do not format a card containing needed files.
Stock recovery's saved fstab maps `/dev/block/mmcblk1p1` to `/sdcard`.

The current firmware backup includes 30 partitions but excludes userdata/cache.
The first backup and the replacement backup are usable after checksum checks.
The interrupted `20261012-182517` backup is damaged and must not be used.

Prepared recovery packages under `dist/gateway-recovery`:

- `backup-userdata-to-microsd-v2.zip`: reads unmounted userdata/cache into
  `/sdcard/kaiforward-stock-backup`, records SHA-256 hashes, and writes no phone
  block device. It refuses insufficient free space, mounted userdata, or an
  existing backup directory. Partial images keep a `.partial` suffix.
- `restore-stock-12.00.17.10-v2.zip`: restores only original boot/system images
  from the fully verified `20261012-123621` backup.
- `restore-userdata-from-microsd-v2.zip`: separately restores original userdata
  and cache from that SD backup, verifies hashes and exact partition sizes,
  and refuses mounted data/cache. **This overwrites current user data.**

Before installing experimental firmware:

1. Validate all produced images, modules and recovery ZIP signatures. Copy the
   stock restore ZIP and backup ZIP onto the microSD card; retain PC copies.
2. Enter stock recovery, run the **backup** ZIP, and return to stock KaiOS.
   Recovery navigation/application still needs a supervised hardware check;
   preparation and signature verification do not prove recovery execution.
3. Copy the resulting userdata/cache images and `SHA256SUMS` to the PC. Verify
   hashes and sizes: userdata `2634006016`, cache `209715200` bytes. Preserve
   the originals. Confirm the keypad recovery-entry path works independently
   of ADB, and confirm the original boot/system ZIP is available on microSD.
4. Review the concrete experimental boot/system ZIP and obtain installation
   authorization. Its updater writes only those two partitions, requests no
   wipe, and checks the Nokia device property before writing. Stock v12
   recovery is retained. Device-specific radio and modem partitions remain
   untouched; boot/system compatibility with the current modem is still a
   hardware validation requirement.
5. Install from recovery, reboot fully, check ADB/root, both SIMs, Wi-Fi, SMS,
   manual calls, DTMF and interactive USSD. Confirm heartbeat after reboot.
   Test the six-second injection on an owner-established call, then capture
   and injection together on each SIM. Stop for crashes or loss of basic
   cellular behavior. A caller must hear the phrase to establish success.

Upstream Gerda instructions suggest a factory reset when changing systems.
This package deliberately does not request one. Existing stock userdata may
be incompatible; do not wipe or let a recovery problem erase the only backup.
Any reset needs its own decision after the offline backup is verified.

If the experimental system fails, enter the retained stock recovery using
the keypad and restore original boot/system. If Gerda altered the data profile
or a reset occurred, restore userdata/cache with the separate SD restore
package while still in recovery. Reboot into stock and repeat the basic SIM,
Wi-Fi and SMS checks. Restoration packages are statically verified, but this
recovery workflow has not yet been demonstrated on the hardware.

Do not run upstream `make all`, `make deploy`, or its generic installer: those
recipes also write radio partitions and clear modem state. The upstream build
and stock-v12 recovery support are described in the
[Gerda source README](https://gitlab.com/project-pris/system/-/blob/0b8d4b1e99efb570e6770b59384c850c1b788d96/README.md).
