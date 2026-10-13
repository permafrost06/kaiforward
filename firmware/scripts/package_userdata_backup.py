#!/usr/bin/env python3
"""Prepare an offline userdata-to-microSD backup ZIP; never run it on the phone."""
import argparse
import os
import re
from pathlib import Path
import subprocess
import tempfile
import zipfile
from recovery_device import DEVICE_CHECK

SCRIPT = '''ui_print("KaiForward offline userdata backup to microSD");
''' + DEVICE_CHECK + '''
ui_print("Preparing offline data/cache mounts...");
unmount("/data");
unmount("/cache");
mount("vfat", "EMMC", "/dev/block/mmcblk1p1", "/sdcard");
assert(package_extract_file("busybox", "/tmp/busybox"));
assert(package_extract_file("backup.sh", "/tmp/kaiforward-backup.sh"));
set_perm(0, 0, 0755, "/tmp/busybox");
ui_print("Starting backup; this may take several minutes...");
assert(run_program("/tmp/busybox", "sh", "/tmp/kaiforward-backup.sh") == 0);
ui_print("Backup completed on microSD. Verify its hashes on the PC before flashing.");
'''
BACKUP = '''#!/tmp/busybox sh
set -eu
BB=/tmp/busybox
umask 077
# Refuse to copy a filesystem still mounted in recovery.
echo 'PHASE: checking offline filesystems'
for MOUNT in /data /cache; do
    if "$BB" awk -v destination="$MOUNT" '$2 == destination { found=1 } END { exit !found }' /proc/mounts; then
        echo "Refusing a live backup: $MOUNT is mounted" >&2
        exit 1
    fi
done
echo 'PHASE: checking physical microSD mount'
if ! "$BB" awk '$1 == "/dev/block/mmcblk1p1" && $2 == "/sdcard" && $3 == "vfat" { found=1 } END { exit !found }' /proc/mounts; then
    echo 'Expected physical microSD is not mounted as FAT at /sdcard' >&2
    "$BB" cat /proc/mounts >&2
    exit 1
fi
AVAILABLE=$("$BB" df -k /sdcard | "$BB" awk 'NR == 2 { print $4 }')
case "$AVAILABLE" in ''|*[!0-9]*) echo 'Unable to read microSD free space' >&2; exit 1;; esac
[ "$AVAILABLE" -ge 4194304 ] || { echo 'Need at least 4 GiB free on microSD' >&2; exit 1; }
DIRECTORY=/sdcard/kaiforward-stock-backup
[ ! -e "$DIRECTORY" ] || { echo 'Backup directory already exists; preserve or move it first' >&2; exit 1; }
"$BB" mkdir "$DIRECTORY"
for NAME in userdata cache; do
    echo "PHASE: copying $NAME"
    "$BB" dd if="/dev/block/bootdevice/by-name/$NAME" of="$DIRECTORY/$NAME.img.partial" bs=1048576
    "$BB" sync
    "$BB" mv "$DIRECTORY/$NAME.img.partial" "$DIRECTORY/$NAME.img"
done
cd "$DIRECTORY"
echo 'PHASE: hashing completed backup files'
"$BB" sha256sum userdata.img cache.img > SHA256SUMS
"$BB" sync
echo 'Offline userdata and cache backup completed'
'''

RESTORE = '''#!/tmp/busybox sh
set -eu
BB=/tmp/busybox
for MOUNT in /data /cache; do
    if "$BB" awk -v destination="$MOUNT" '$2 == destination { found=1 } END { exit !found }' /proc/mounts; then
        echo 'Refusing to restore a mounted filesystem' >&2
        exit 1
    fi
done
"$BB" awk '$1 == "/dev/block/mmcblk1p1" && $2 == "/sdcard" && $3 == "vfat" { found=1 } END { exit !found }' /proc/mounts
cd /sdcard/kaiforward-stock-backup
"$BB" sha256sum -c SHA256SUMS
[ "$("$BB" stat -c %s userdata.img)" = 2634006016 ]
[ "$("$BB" stat -c %s cache.img)" = 209715200 ]
for NAME in userdata cache; do
    EXPECTED=$("$BB" stat -c %s "$NAME.img")
    [ "$("$BB" blockdev --getsize64 "/dev/block/bootdevice/by-name/$NAME")" = "$EXPECTED" ]
done
for NAME in userdata cache; do
    "$BB" dd if="$NAME.img" of="/dev/block/bootdevice/by-name/$NAME" bs=1048576
    "$BB" sync
    EXPECTED=$("$BB" sha256sum "$NAME.img")
    ACTUAL=$("$BB" sha256sum "/dev/block/bootdevice/by-name/$NAME")
    [ "${EXPECTED%% *}" = "${ACTUAL%% *}" ]
done
echo 'Original userdata and cache restored'
'''


def package(gerda, output, restore=False, backup_directory='kaiforward-stock-backup'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}', backup_directory):
        raise ValueError('Backup directory must be a simple microSD directory name')
    if output.exists():
        raise ValueError('Refusing to replace an existing package')
    output.parent.mkdir(parents=True, exist_ok=True)
    script, shell = SCRIPT, BACKUP
    if restore:
        script = SCRIPT.replace('offline userdata backup to microSD', 'RESTORE userdata/cache FROM microSD')
        script = script.replace('Starting backup; this may take several minutes...',
                                'Starting userdata/cache restore; this may take several minutes...')
        script = script.replace('Backup completed on microSD. Verify its hashes on the PC before flashing.',
                                'Original userdata/cache restored. Stock boot/system must also be restored.')
        shell = RESTORE
    shell = shell.replace('/sdcard/kaiforward-stock-backup', '/sdcard/' + backup_directory)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='userdata-zip-') as temporary:
        unsigned, signed = Path(temporary) / 'unsigned.zip', Path(temporary) / 'signed.zip'
        with zipfile.ZipFile(unsigned, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(gerda / 'tools/stockerize/update-binary', 'META-INF/com/google/android/update-binary')
            archive.write(gerda / 'src/system/bin/busybox', 'busybox')
            archive.writestr('META-INF/com/google/android/updater-script', script)
            archive.writestr('backup.sh', shell)
        subprocess.run([os.environ.get('GATEWAY_JAVA', 'java'), '-Xmx1024m', '-jar',
                        str(gerda / 'tools/stockerize/signapk.jar'), '-w',
                        str(gerda / 'tools/keys/testkey.x509.pem'), str(gerda / 'tools/keys/testkey.pk8'),
                        str(unsigned), str(signed)], check=True)
        with zipfile.ZipFile(signed) as archive:
            if archive.testzip() or archive.read('backup.sh').decode() != shell:
                raise ValueError('Signed backup ZIP failed verification')
        signed.rename(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gerda', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--restore', action='store_true', help='Prepare an explicit userdata/cache restoration package')
    parser.add_argument('--backup-directory', default='kaiforward-stock-backup', help='Use a separate directory to preserve previous backups')
    args = parser.parse_args()
    package(args.gerda.resolve(), args.output.resolve(), args.restore, args.backup_directory)
    print(args.output)
