#!/usr/bin/env python3
"""Prepare a call-audio kernel trial, preserving the working native ramdisk.

Writes PC artifacts only. The signed restore package retains the original
native boot and modules; neither package changes modem or user data.
"""
import argparse
import gzip
import json
import os
from pathlib import Path
import shutil
import struct
import tarfile
import tempfile
import zipfile

from build_gateway_image import BOOT_SIZE, digest, run, kernel_with_original_dtb
from validate_images import cpio_files
from validate_modules import validate as validate_modules
from recovery_device import DEVICE_CHECK

ROOT = Path(__file__).resolve().parents[2]


def sign(gerda, boot, modules, hashes, output):
    # Recovery supplies the busybox utility and mounts system explicitly.
    script = '''#!/tmp/busybox sh
set -eu
BB=/tmp/busybox
BOOT=/dev/block/bootdevice/by-name/boot
SYSTEM=/dev/block/bootdevice/by-name/system
[ "$($BB blockdev --getsize64 "$BOOT")" = 26165248 ]
[ "$($BB blockdev --getsize64 "$SYSTEM")" = 838860800 ]
$BB sha256sum -c /tmp/payload.sha256
$BB mkdir -p /system
if $BB awk '$2 == "/system" { found=1 } END { exit !found }' /proc/mounts; then
    $BB mount -o remount,rw /system
else
    $BB mount -t ext4 "$SYSTEM" /system
fi
[ -f /system/bin/kaiforward-native ]
if [ -e /system/lib/modules.call-new ] || [ -e /system/lib/modules.call-old ]; then
    echo 'Prior module transaction needs inspection'; exit 1
fi
$BB mkdir /system/lib/modules.call-new
$BB tar -xzf /tmp/modules.tar.gz -C /system/lib/modules.call-new
$BB chmod 0755 /system/lib/modules.call-new
$BB chown -R 0:0 /system/lib/modules.call-new
$BB mv /system/lib/modules /system/lib/modules.call-old
$BB mv /system/lib/modules.call-new /system/lib/modules
$BB sync
$BB dd if=/tmp/boot.img of="$BOOT" bs=1048576
$BB sync
ACTUAL=$($BB sha256sum "$BOOT")
[ "${ACTUAL%% *}" = BOOT_HASH ]
$BB rm -rf /system/lib/modules.call-old
$BB sync
echo 'Native boot and matching modules installed; radio/data untouched'
'''.replace('BOOT_HASH', digest(boot))
    run(['sh', '-n'], input=script.encode())
    updater = 'ui_print("KaiForward native call-audio boot/modules package");\n' + DEVICE_CHECK
    for name in ['busybox', 'boot.img', 'modules.tar.gz', 'payload.sha256', 'install.sh']:
        updater += f'assert(package_extract_file("{name}", "/tmp/{name}"));\n'
    updater += '''set_perm(0, 0, 0755, "/tmp/busybox");
assert(run_program("/tmp/busybox", "sh", "/tmp/install.sh") == 0);
ui_print("Native boot/modules installed and boot readback verified.");
'''
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        unsigned = Path(temporary) / 'unsigned.zip'
        with zipfile.ZipFile(unsigned, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.write(gerda / 'tools/stockerize/update-binary', 'META-INF/com/google/android/update-binary')
            archive.write(gerda / 'src/system/bin/busybox', 'busybox')
            archive.write(boot, 'boot.img')
            archive.write(modules, 'modules.tar.gz')
            archive.writestr('payload.sha256', hashes)
            archive.writestr('install.sh', script)
            archive.writestr('META-INF/com/google/android/updater-script', updater)
        run([os.environ.get('GATEWAY_JAVA', 'java'), '-Xmx1024m', '-jar',
             gerda / 'tools/stockerize/signapk.jar', '-w', gerda / 'tools/keys/testkey.x509.pem',
             gerda / 'tools/keys/testkey.pk8', unsigned, output])
    with zipfile.ZipFile(output) as archive:
        if archive.testzip():
            raise ValueError('Recovery package CRC failure')


def pack_modules(directory, destination):
    with tarfile.open(destination, 'w:gz') as archive:
        for path in sorted(directory.rglob('*')):
            archive.add(path, arcname=path.relative_to(directory), recursive=False)


def prepare(gerda, build, source, wlan, original_boot, original_modules, output):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Preserve previous trial artifacts; output must be empty')
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    replacement = ROOT / 'firmware/kernel/kaiforward_incall_playback.h'
    if replacement.read_bytes() != (source / 'sound/soc/msm/qdsp6v2/kaiforward_incall_playback.h').read_bytes():
        raise ValueError('Reviewed kernel replacement differs from built source')
    if b'kaiforward: incall playback' not in (build / 'vmlinux').read_bytes():
        raise ValueError('Kernel lacks session-aware playback implementation')
    raw = original_boot.read_bytes()
    if len(raw) != BOOT_SIZE or raw[:8] != b'ANDROID!':
        raise ValueError('Unexpected original native boot')
    kernel_size, _, ramdisk_size, _, _, _, _, page = struct.unpack_from('<8I', raw, 8)
    offset = page + (kernel_size + page - 1) // page * page
    ramdisk_bytes = raw[offset:offset + ramdisk_size]
    files = cpio_files(gzip.decompress(ramdisk_bytes))
    if any(files[name][0] & 0o777 != 0o755 for name in ['.', 'data', 'dev', 'system']):
        raise ValueError('Working native ramdisk traversal modes are missing')
    if b'service kf-permission ' not in files['init.kaiforward-native.rc'][1]:
        raise ValueError('Working native audio permission startup is missing')
    combined, ramdisk = output / 'zImage-dtb', output / 'ramdisk.gz'
    dtb = kernel_with_original_dtb(build / 'arch/arm/boot/zImage', gerda / 'src/boot/8110/zImage', combined)
    ramdisk.write_bytes(ramdisk_bytes)
    run([gerda / 'tools/abootimg/abootimg', '--create', output / 'boot.img', '-f',
         gerda / 'src/boot/8110/bootimg.cfg', '-k', combined, '-r', ramdisk])
    if (output / 'boot.img').stat().st_size != BOOT_SIZE:
        raise ValueError('New boot size differs from partition')
    modules = sorted(build.rglob('*.ko'))
    if len({p.name for p in modules}) != len(modules):
        raise ValueError('Duplicate module names')
    if not {'mcDrvModule.ko', 'mcKernelApi.ko'}.issubset(p.name for p in modules):
        raise ValueError('Critical modules missing')
    validation = validate_modules(build, modules + [wlan])
    target = output / 'modules'
    (target / 'pronto').mkdir(parents=True)
    objcopy = os.environ.get('GATEWAY_OBJCOPY', '/home/frost/opt/zephyr-sdk-0.16.9/arm-zephyr-eabi/bin/arm-zephyr-eabi-objcopy')
    for path in modules + [wlan]:
        destination = target / ('pronto/pronto_wlan.ko' if path == wlan else path.name)
        shutil.copyfile(path, destination)
        run([objcopy, '--strip-debug', destination])
        destination.chmod(0o644)
    (target / 'wlan.ko').symlink_to('/system/lib/modules/pronto/pronto_wlan.ko')
    target.chmod(0o755)
    (target / 'pronto').chmod(0o755)
    validate_modules(build, [p for p in target.rglob('*.ko') if not p.is_symlink()])
    pack_modules(target, output / 'modules.tar.gz')
    shutil.copyfile(original_boot, output / 'restore-boot.img')
    pack_modules(original_modules, output / 'restore-modules.tar.gz')
    for prefix, zip_name in [('', 'install-call-kernel.zip'), ('restore-', 'restore-native-kernel.zip')]:
        boot, archive = output / (prefix + 'boot.img'), output / (prefix + 'modules.tar.gz')
        hashes = digest(boot) + '  /tmp/boot.img\n' + digest(archive) + '  /tmp/modules.tar.gz\n'
        sign(gerda, boot, archive, hashes, output / zip_name)
    report = {'scope': ['boot', 'system/lib/modules'], 'modem_changed': False,
              'ramdisk_bytes_preserved': True, 'ramdisk_sha256': digest(ramdisk),
              'original_boot_sha256': digest(original_boot), 'boot_sha256': digest(output / 'boot.img'),
              'kernel_release': (build / 'include/config/kernel.release').read_text().strip(),
              'modules': {str(p.relative_to(target)): digest(p) for p in target.rglob('*.ko') if not p.is_symlink()},
              'module_validation': validation, 'dtb': dtb,
              'install_package_sha256': digest(output / 'install-call-kernel.zip'),
              'restore_package_sha256': digest(output / 'restore-native-kernel.zip'),
              'hardware_boot_and_audible_injection': 'not yet tested'}
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gerda', type=Path, default=ROOT / 'data/gateway-build/gerda')
    p.add_argument('--kernel-build', type=Path, default=ROOT / 'data/gateway-build/kernel-out-gcc48')
    p.add_argument('--kernel-source', type=Path, default=ROOT / 'data/gateway-build/kernel')
    p.add_argument('--wlan', type=Path, default=ROOT / 'data/gateway-build/kernel/drivers/net/wireless/prima/wlan.ko')
    p.add_argument('--original-boot', type=Path, required=True)
    p.add_argument('--original-modules', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(prepare(a.gerda, a.kernel_build, a.kernel_source, a.wlan, a.original_boot,
                             a.original_modules, a.output), indent=2))
