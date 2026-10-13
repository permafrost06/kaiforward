#!/usr/bin/env python3
"""Repair native ramdisk traversal/init names, keeping kernel and system unchanged."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import zipfile

from build_gateway_image import BOOT_SIZE, run, digest
from validate_images import cpio_files
from recovery_device import DEVICE_CHECK

PROJECT = Path(__file__).resolve().parents[2]
DIRECTORIES = {'.', 'data', 'dev', 'oem', 'proc', 'sys', 'system'}


def repair_cpio(raw, startup):
    offset = 0
    output = bytearray()
    seen = set()
    while raw[offset:offset + 6] == b'070701':
        header = raw[offset:offset + 110]
        fields = [int(header[6 + 8*i:14 + 8*i], 16) for i in range(13)]
        name_bytes = raw[offset + 110:offset + 110 + fields[11]]
        name = name_bytes[:-1].decode().removeprefix('./')
        start = (offset + 110 + fields[11] + 3) & ~3
        content = raw[start:start + fields[6]]
        next_offset = (start + fields[6] + 3) & ~3
        if name in DIRECTORIES:
            fields[1] = (fields[1] & ~0o777) | 0o755
            seen.add(name)
        if name == 'init.kaiforward-native.rc':
            content = startup
            fields[1] = 0o100644
            fields[6] = len(content)
            seen.add(name)
        output.extend(b'070701' + ''.join(f'{v:08x}' for v in fields).encode())
        output.extend(name_bytes)
        output.extend(b'\0' * (-len(output) % 4))
        output.extend(content)
        output.extend(b'\0' * (-len(output) % 4))
        if name == 'TRAILER!!!':
            if seen != DIRECTORIES | {'init.kaiforward-native.rc'}:
                raise ValueError('Expected native ramdisk entries missing')
            files = cpio_files(bytes(output))
            assert all(files[n][0] & 0o777 == 0o755 for n in DIRECTORIES)
            return bytes(output)
        offset = next_offset
    raise ValueError('Malformed/truncated ramdisk')


def package(gerda, original, output):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Preserve existing repair artifacts; output must be empty')
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = original.read_bytes()
    if len(raw) != BOOT_SIZE or raw[:8] != b'ANDROID!':
        raise ValueError('Unexpected original native boot image')
    kernel_size, _, ramdisk_size, _, _, _, _, page = struct.unpack_from('<8I', raw, 8)
    offset = page + ((kernel_size + page - 1) // page) * page
    startup = (PROJECT / 'firmware/native/init.native.rc').read_bytes()
    if b'service kf-upload ' not in startup:
        raise ValueError('Corrected short upload service name is missing')
    fixed = repair_cpio(gzip.decompress(raw[offset:offset + ramdisk_size]), startup)
    with tempfile.TemporaryDirectory(dir=output, prefix='boot-repair-') as tmp:
        work = Path(tmp)
        kernel = work / 'kernel'
        kernel.write_bytes(raw[page:page + kernel_size])
        ramdisk = work / 'ramdisk.gz'
        ramdisk.write_bytes(gzip.compress(fixed, compresslevel=9, mtime=0))
        boot = output / 'boot.img'
        run([gerda / 'tools/abootimg/abootimg', '--create', boot, '-f',
             gerda / 'src/boot/8110/bootimg.cfg', '-k', kernel, '-r', ramdisk])
        if boot.stat().st_size != BOOT_SIZE:
            raise ValueError('Repair boot size differs from phone partition')
        rebuilt = boot.read_bytes()
        rebuilt_kernel_size = struct.unpack_from('<I', rebuilt, 8)[0]
        if rebuilt_kernel_size != kernel_size or rebuilt[page:page + kernel_size] != kernel.read_bytes():
            raise ValueError('Kernel/DTBs changed during ramdisk repair')
        checksum = digest(boot)
        shell = f'''#!/tmp/busybox sh
set -eu
BB=/tmp/busybox
TARGET=/dev/block/bootdevice/by-name/boot
[ "$($BB blockdev --getsize64 "$TARGET")" = {BOOT_SIZE} ]
RESOLVED=$($BB readlink -f "$TARGET")
for SOURCE in $($BB awk '$1 ~ /^\\/dev\\// {{ print $1 }}' /proc/mounts); do
    [ "$($BB readlink -f "$SOURCE")" != "$RESOLVED" ] || exit 1
done
EXPECTED={checksum}
ACTUAL=$($BB sha256sum /tmp/native-boot.img)
[ "${{ACTUAL%% *}}" = "$EXPECTED" ]
echo 'Writing corrected native boot ramdisk; system and radio untouched'
$BB dd if=/tmp/native-boot.img of="$TARGET" bs=1048576
$BB sync
ACTUAL=$($BB sha256sum "$TARGET")
[ "${{ACTUAL%% *}}" = "$EXPECTED" ]
echo 'Corrected boot readback hash verified'
'''
        run(['sh', '-n'], input=shell.encode())
        script = 'ui_print("KaiForward native boot repair: directory traversal and upload startup");\n' + DEVICE_CHECK + '''
assert(package_extract_file("busybox", "/tmp/busybox"));
assert(package_extract_file("boot.img", "/tmp/native-boot.img"));
assert(package_extract_file("repair.sh", "/tmp/native-repair.sh"));
set_perm(0, 0, 0755, "/tmp/busybox");
assert(run_program("/tmp/busybox", "sh", "/tmp/native-repair.sh") == 0);
ui_print("Native boot repair completed; boot readback verified.");
'''
        unsigned, signed = work / 'unsigned.zip', output / 'repair-native-boot.zip'
        with zipfile.ZipFile(unsigned, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(gerda / 'tools/stockerize/update-binary', 'META-INF/com/google/android/update-binary')
            archive.write(gerda / 'src/system/bin/busybox', 'busybox')
            archive.write(boot, 'boot.img')
            archive.writestr('repair.sh', shell)
            archive.writestr('META-INF/com/google/android/updater-script', script)
        run([os.environ.get('GATEWAY_JAVA', 'java'), '-Xmx1024m', '-jar',
             gerda / 'tools/stockerize/signapk.jar', '-w', gerda / 'tools/keys/testkey.x509.pem',
             gerda / 'tools/keys/testkey.pk8', unsigned, signed])
        with zipfile.ZipFile(signed) as archive:
            if archive.testzip() or archive.read('repair.sh').decode() != shell:
                raise ValueError('Repair ZIP content check failed')
            if hashlib.sha256(archive.read('boot.img')).hexdigest() != checksum:
                raise ValueError('Packaged repair boot hash mismatch')
    report = {'partition_scope': ['boot'], 'original_boot_sha256': digest(original),
              'fixed_boot_sha256': checksum, 'kernel_and_dtbs_unchanged': True,
              'system_unchanged': True, 'ramdisk_root_and_mountpoints': '0755',
              'upload_service': 'kf-upload', 'package_sha256': digest(signed)}
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gerda', type=Path, default=PROJECT / 'data/gateway-build/gerda')
    parser.add_argument('--original-boot', type=Path, default=PROJECT / 'dist/gerda-native-20261013/boot.img')
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist/gerda-native-boot-repair')
    args = parser.parse_args()
    print(json.dumps(package(args.gerda.resolve(), args.original_boot.resolve(), args.output.resolve()), indent=2))
