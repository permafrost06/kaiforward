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

The PC SMS receiver and browser relay are not included in this firmware port.
The SMS service uses the PoC `/v1/messages` receiver protocol. The call helper
connects to the same configured host over TLS on port 8769 by default. Both
use the provisioned device ID/token; the relay expects the PoC call protocol.

Prepare private settings with:

```sh
python3 -m scripts.configure_native \
  --ssid 'Your Wi-Fi network' \
  --receiver-url 'https://receiver.example/v1/messages' \
  --receiver-config /path/to/private-receiver-config.json \
  --ca /path/to/receiver-ca.pem
```

The receiver config contains `token` and `device_id`. Passwords are prompted
without echoing. Add `--install --serial <serial>` to provision an installed
native phone through root ADB. This writes settings and requests a reload.
The keypad UI can edit Wi-Fi and destination settings; certificates are
provisioned from the PC.

## Checks and licenses

```sh
python3 -m unittest discover -s tests -v
```

Host tests need GCC, SQLite development headers, curl and OpenSSL. They use
synthetic modem sockets and a receiver fixture under `tests/support/`, with
no phone connection. They cover SMS decoding/durability/retries/TLS, call
control, Wi-Fi configuration, Binder permissions, startup and kernel session
selection. They do not prove carrier or hardware behavior.

The embedded font retains its license in `native/FONT-LICENSE.txt`. SQLite is
public domain and Mbed TLS is Apache-2.0 OR GPL-2.0-or-later. The Linux kernel
and its integrated playback patch are GPL-2.0-only; Gerda's system includes
third-party components with their respective upstream licenses. Downloaded
sources retain their license files. These component licenses also apply when
distributing firmware images; the root MIT license covers KaiForward's own
userspace code.
