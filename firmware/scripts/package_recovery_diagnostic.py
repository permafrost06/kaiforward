#!/usr/bin/env python3
"""Build a signed log collector; write only temporary files and microSD logs."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

from recovery_device import DEVICE_CHECK

SCRIPT = '''ui_print("KaiForward recovery diagnostics: no partition writes");
''' + DEVICE_CHECK + '''
mount("vfat", "EMMC", "/dev/block/mmcblk1p1", "/sdcard");
assert(package_extract_file("busybox", "/tmp/busybox"));
assert(package_extract_file("diagnostic.sh", "/tmp/kaiforward-diagnostic.sh"));
set_perm(0, 0, 0755, "/tmp/busybox");
assert(run_program("/tmp/busybox", "sh", "/tmp/kaiforward-diagnostic.sh") == 0);
ui_print("Recovery logs saved under /sdcard/kaiforward-recovery-diagnostics.");
ui_print("Diagnostic collection completed.");
'''

SHELL = '''#!/tmp/busybox sh
set -eu
BB=/tmp/busybox
umask 077
"$BB" awk '$1 == "/dev/block/mmcblk1p1" && $2 == "/sdcard" && $3 == "vfat" { found=1 } END { exit !found }' /proc/mounts
BASE=/sdcard/kaiforward-recovery-diagnostics
"$BB" mkdir -p "$BASE"
DIRECTORY="$BASE/$("$BB" date +%Y%m%d-%H%M%S)-$$"
"$BB" mkdir "$DIRECTORY"
for SOURCE in /tmp/recovery.log /tmp/last_install /cache/recovery/log /cache/recovery/last_log /cache/recovery/last_install /cache/recovery/last_kmsg /proc/last_kmsg /proc/mounts /proc/partitions /proc/cmdline /proc/version /default.prop /etc/recovery.fstab; do
    if [ -r "$SOURCE" ]; then
        NAME=$(echo "$SOURCE" | "$BB" tr / _)
        "$BB" cat "$SOURCE" > "$DIRECTORY/$NAME" || true
    fi
done
"$BB" dmesg > "$DIRECTORY/dmesg.txt" 2>&1 || true
"$BB" ls -l /dev/block/bootdevice/by-name > "$DIRECTORY/partition-links.txt" 2>&1 || true
for NAME in boot recovery; do
    "$BB" sha256sum "/dev/block/bootdevice/by-name/$NAME" >> "$DIRECTORY/partition-sha256.txt" 2>&1 || true
done
"$BB" grep -i -E 'E:|failed|error|abort' /tmp/recovery.log | "$BB" tail -n 6 | "$BB" cut -c 1-160 > /tmp/kaiforward-recovery-errors.txt || true
"$BB" sync
echo "Diagnostic logs saved: $DIRECTORY"
'''


def package(gerda, output):
    if output.exists():
        raise ValueError('Refusing to replace an existing package')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix='diagnostic-zip-') as temporary:
        unsigned, signed = Path(temporary) / 'unsigned.zip', Path(temporary) / 'signed.zip'
        with zipfile.ZipFile(unsigned, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(gerda / 'tools/stockerize/update-binary', 'META-INF/com/google/android/update-binary')
            archive.write(gerda / 'src/system/bin/busybox', 'busybox')
            archive.writestr('META-INF/com/google/android/updater-script', SCRIPT)
            archive.writestr('diagnostic.sh', SHELL)
        subprocess.run([os.environ.get('GATEWAY_JAVA', 'java'), '-Xmx1024m', '-jar',
                        str(gerda / 'tools/stockerize/signapk.jar'), '-w',
                        str(gerda / 'tools/keys/testkey.x509.pem'), str(gerda / 'tools/keys/testkey.pk8'),
                        str(unsigned), str(signed)], check=True)
        with zipfile.ZipFile(signed) as archive:
            if archive.testzip() or archive.read('diagnostic.sh').decode() != SHELL:
                raise ValueError('Signed diagnostic ZIP failed verification')
        signed.rename(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gerda', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    package(args.gerda.resolve(), args.output.resolve())
    print(args.output)
