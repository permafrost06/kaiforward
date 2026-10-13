#!/usr/bin/env python3
"""Build a small signed restore ZIP; boot/system image data stays on microSD."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

from recovery_device import DEVICE_CHECK

SCRIPT = '''ui_print("KaiForward ORIGINAL STOCK boot/system restore from microSD");
''' + DEVICE_CHECK + '''
unmount("/system");
mount("vfat", "EMMC", "/dev/block/mmcblk1p1", "/sdcard");
assert(package_extract_file("busybox", "/tmp/busybox"));
assert(package_extract_file("restore.sh", "/tmp/kaiforward-stock-restore.sh"));
assert(package_extract_file("SHA256SUMS", "/tmp/kaiforward-stock-SHA256SUMS"));
set_perm(0, 0, 0755, "/tmp/busybox");
ui_print("Checking original images; restoring system then boot. Please wait.");
assert(run_program("/tmp/busybox", "sh", "/tmp/kaiforward-stock-restore.sh") == 0);
ui_print("ORIGINAL STOCK system and boot restored; readback hashes verified.");
ui_print("Userdata preserved. Select Reboot system now.");
'''

SHELL = '''#!/tmp/busybox sh
set -eu
BB=/tmp/busybox
DIRECTORY=/sdcard/kaiforward-stock-restore
"$BB" awk '$1 == "/dev/block/mmcblk1p1" && $2 == "/sdcard" && $3 == "vfat" { found=1 } END { exit !found }' /proc/mounts
cd "$DIRECTORY"
LOG="restore-$("$BB" date +%Y%m%d-%H%M%S)-$$.log"
exec > "$LOG" 2>&1
echo 'Verifying original stock image hashes before any partition writes'
"$BB" sha256sum -c /tmp/kaiforward-stock-SHA256SUMS
[ "$("$BB" stat -c %s system.img)" = 838860800 ]
[ "$("$BB" stat -c %s boot.img)" = 26165248 ]
# Validate BOTH targets before writing either. Refuse any mounted target alias.
for NAME in system boot; do
    TARGET=/dev/block/bootdevice/by-name/$NAME
    "$BB" test -b "$TARGET"
    EXPECTED_SIZE=$("$BB" stat -c %s "$NAME.img")
    [ "$("$BB" blockdev --getsize64 "$TARGET")" = "$EXPECTED_SIZE" ]
    RESOLVED=$("$BB" readlink -f "$TARGET")
    [ -n "$RESOLVED" ]
    if "$BB" awk '$2 == "/system" || $2 == "/boot" { found=1 } END { exit !found }' /proc/mounts; then
        echo 'Refusing restoration: system or boot is mounted'
        exit 1
    fi
    for SOURCE in $("$BB" awk '$1 ~ /^\\/dev\\// { print $1 }' /proc/mounts); do
        if [ "$("$BB" readlink -f "$SOURCE")" = "$RESOLVED" ]; then
            echo "Refusing restoration: $NAME target is mounted through $SOURCE"
            exit 1
        fi
    done
done
# Restore and verify system first; write boot only after verified system success.
for NAME in system boot; do
    TARGET=/dev/block/bootdevice/by-name/$NAME
    echo "Writing original stock $NAME"
    "$BB" dd if="$NAME.img" of="$TARGET" bs=1048576
    "$BB" sync
    EXPECTED=$("$BB" awk -v image="$NAME.img" '$2 == image { print $1 }' /tmp/kaiforward-stock-SHA256SUMS)
    [ -n "$EXPECTED" ]
    ACTUAL=$("$BB" sha256sum "$TARGET")
    [ "${ACTUAL%% *}" = "$EXPECTED" ]
    echo "Verified original stock $NAME readback: $EXPECTED"
done
echo 'Stock boot/system restore completed; userdata/cache/radio/recovery untouched'
"$BB" sync
'''


def package(gerda, backup, output):
    if output.exists():
        raise ValueError('Refusing to replace an existing package')
    manifest = json.loads((backup / 'manifest.json').read_text())
    if manifest['device'] != 'TA-1059' or manifest['serial'] != '22aaa475':
        raise ValueError('Unexpected backup device')
    checksums = ''
    for name, size in [('system', 838860800), ('boot', 26165248)]:
        image = backup / (name + '.img')
        expected = manifest['partitions'][name]
        if image.stat().st_size != size or expected['bytes'] != size:
            raise ValueError('Unexpected image size: ' + name)
        with image.open('rb') as source:
            actual = hashlib.file_digest(source, 'sha256').hexdigest()
        if actual != expected['sha256']:
            raise ValueError('Original backup hash mismatch: ' + name)
        checksums += actual + '  ' + name + '.img\n'
    subprocess.run(['sh', '-n'], input=SHELL.encode(), check=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='stock-sd-zip-') as temporary:
        unsigned, signed = Path(temporary) / 'unsigned.zip', Path(temporary) / 'signed.zip'
        with zipfile.ZipFile(unsigned, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(gerda / 'tools/stockerize/update-binary', 'META-INF/com/google/android/update-binary')
            archive.write(gerda / 'src/system/bin/busybox', 'busybox')
            archive.writestr('META-INF/com/google/android/updater-script', SCRIPT)
            archive.writestr('restore.sh', SHELL)
            archive.writestr('SHA256SUMS', checksums)
        subprocess.run([os.environ.get('GATEWAY_JAVA', 'java'), '-Xmx1024m', '-jar',
                        str(gerda / 'tools/stockerize/signapk.jar'), '-w',
                        str(gerda / 'tools/keys/testkey.x509.pem'), str(gerda / 'tools/keys/testkey.pk8'),
                        str(unsigned), str(signed)], check=True)
        with zipfile.ZipFile(signed) as archive:
            if archive.testzip() or archive.read('restore.sh').decode() != SHELL or archive.read('SHA256SUMS').decode() != checksums:
                raise ValueError('Signed restore ZIP failed verification')
        signed.rename(output)
    return checksums


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gerda', required=True, type=Path)
    parser.add_argument('--backup', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(package(args.gerda.resolve(), args.backup.resolve(), args.output.resolve()))
