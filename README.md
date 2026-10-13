# KaiForward

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

`scripts.flasher` builds a small signed package for the phone's stock recovery.
The package writes `system`, then `boot`, from images kept on the microSD
card:

```sh
python3 -m scripts.flasher <images-dir> --name <name> \
    --backup backups/<dir> --push
```

`<images-dir>` holds `boot.img` and/or `system.img`; `--partitions boot` limits
the package to one of them. The result goes into `dist/<name>/`, laid out as it
goes on the card: `<name>.zip` plus a `<name>/` folder with the images.
`--push` copies both into `kaiforward/` on the phone's card over ADB and checks
their hashes on the phone. Without `--push`, copy the contents of
`dist/<name>/` into a `kaiforward` folder on the card.

To install, power off the phone and hold **D-pad Up** while pressing **Power**
to enter recovery. Choose **Apply update from SD card**, then `kaiforward/<name>.zip`.
When it finishes, choose **Reboot system now**. Recovery writes a log to
`kaiforward/<name>/flash.log` on the card.

The package refuses to write anything unless all of these hold:

- every image matches the hash and size recorded in the package,
- every target partition has the expected size and is not mounted.

After writing each partition, the package drops the page cache and checks the
partition's readback hash. It never writes recovery, radio, bootloader or data
partitions, and does not wipe data.

The build requires a verified backup of the target partitions, so a stock
restore can always be built from it. That backup is also the restore source:

```sh
python3 -m scripts.flasher backups/<dir> --backup backups/<dir> \
    --name restore-stock --push
```

Stock recovery only accepts packages signed with the AOSP test key. The
recovery updater, busybox and test key are in `recovery/`, which records where
each comes from, its license and its source. Signing uses `openssl`, and its
output is byte-identical to AOSP `signapk -w`, so Java is not needed.

## License

KaiForward is licensed under the MIT License; see `LICENSE`. The third-party
files in `recovery/` keep their own licenses, described in
`recovery/README.md`.
