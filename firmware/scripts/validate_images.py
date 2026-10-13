#!/usr/bin/env python3
"""Read-only checks of the assembled boot image, ramdisk, and ext4 filesystem."""
import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path
import struct
import subprocess


def cpio_files(raw):
    offset, files = 0, {}
    while raw[offset:offset + 6] == b'070701':
        header = raw[offset:offset + 110]
        fields = [int(header[6 + 8 * index:14 + 8 * index], 16) for index in range(13)]
        name = raw[offset + 110:offset + 110 + fields[11] - 1].decode()
        start = (offset + 110 + fields[11] + 3) & ~3
        if name == 'TRAILER!!!':
            return files
        if fields[2:4] != [0, 0]:
            raise ValueError('Ramdisk entry is not root-owned: ' + name)
        files[name.removeprefix('./')] = (fields[1], raw[start:start + fields[6]])
        offset = (start + fields[6] + 3) & ~3
    raise ValueError('Malformed ramdisk cpio archive')


def validate(output, gerda, kernel_build):
    manifest = json.loads((output / 'manifest.json').read_text())
    for name, expected in manifest['images'].items():
        path = output / name
        with path.open('rb') as source:
            checksum = hashlib.file_digest(source, 'sha256').hexdigest()
        if path.stat().st_size != expected['bytes'] or checksum != expected['sha256']:
            raise ValueError('Image size/hash mismatch: ' + name)
    raw = (output / 'boot.img').read_bytes()
    if raw[:8] != b'ANDROID!':
        raise ValueError('Invalid Android boot image magic')
    kernel_size, kernel_address, ramdisk_size, ramdisk_address, second_size, _, tags, page, _, _ = struct.unpack_from('<10I', raw, 8)
    if (kernel_address, ramdisk_address, tags, page, second_size) != (0x80008000, 0x81000000, 0x80000100, 2048, 0):
        raise ValueError('Boot layout differs from the inspected Nokia layout')
    kernel = raw[page:page + kernel_size]
    rebuilt = (kernel_build / 'arch/arm/boot/zImage').read_bytes()
    if not kernel.startswith(rebuilt):
        raise ValueError('Boot image does not contain the rebuilt kernel')
    dtbs = kernel[len(rebuilt):]
    if hashlib.sha256(dtbs).hexdigest() != manifest['original_dtb_sha256']:
        raise ValueError('Appended device-tree data changed')
    ramdisk_offset = page + ((kernel_size + page - 1) // page) * page
    files = cpio_files(gzip.decompress(raw[ramdisk_offset:ramdisk_offset + ramdisk_size]))
    for name in ['.', 'data', 'dev', 'oem', 'proc', 'sys', 'system']:
        if files[name][0] & 0o777 != 0o755:
            raise ValueError('Ramdisk root/mountpoint traversal permissions missing: ' + name)
    if not files['init'][0] & 0o111 or not files['sbin/adbd'][0] & 0o111:
        raise ValueError('Init/ADB executable permissions missing')
    if b'import /init.kaiforward.rc\n' not in files['init.b2g.rc'][1]:
        raise ValueError('Gateway init service is not imported')
    project = Path(__file__).resolve().parents[2]
    if files['init.kaiforward.rc'][1] != (project / 'firmware/ramdisk/init.kaiforward.rc').read_bytes():
        raise ValueError('Gateway service definition differs')
    result = subprocess.run(['e2fsck', '-fn', str(output / 'system.img')], capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError('System filesystem check failed: ' + result.stdout + result.stderr)
    for path in ['/kaiforward/health.sh', '/kaiforward/capture_call_audio.sh',
                 '/kaiforward/inject_call_audio.sh', '/kaiforward/injection-test.wav',
                 '/lib/modules/pronto/pronto_wlan.ko', '/b2g/omni.ja']:
        listing = subprocess.run(['debugfs', '-R', 'stat ' + path, str(output / 'system.img')],
                                 check=True, capture_output=True, text=True).stdout
        if 'Inode:' not in listing or 'User:     0' not in listing:
            raise ValueError('Missing/unexpected ownership of system entry: ' + path)
    link = subprocess.run(['debugfs', '-R', 'stat /lib/modules/wlan.ko', str(output / 'system.img')],
                          check=True, capture_output=True, text=True).stdout
    if '/system/lib/modules/pronto/pronto_wlan.ko' not in link:
        raise ValueError('WLAN link does not resolve to the rebuilt driver')
    for name, expected in manifest['modules'].items():
        data = subprocess.run(['debugfs', '-R', 'cat /lib/modules/' + name, str(output / 'system.img')],
                              check=True, capture_output=True).stdout
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError('Packaged module hash mismatch: ' + name)
    for path in ['/bin/busybox', '/bin/sh', '/bin/b2g.sh'] + [
            '/' + item for item in manifest['explicit_executable_permissions']]:
        listing = subprocess.run(['debugfs', '-R', 'stat ' + path, str(output / 'system.img')],
                                 check=True, capture_output=True, text=True).stdout
        mode = re.search(r'Mode:\s+([0-7]+)', listing)
        if not mode or not int(mode[1], 8) & 0o111:
            raise ValueError('Missing executable permission: ' + path)
    return {'boot_layout': 'passed', 'root_owned_ramdisk': 'passed',
            'compiled_kernel_and_original_dtbs': 'passed', 'gateway_service_import': 'passed',
            'ext4_read_only_fsck': 'passed', 'system_files_and_wlan_link': 'passed',
            'packaged_module_hashes': 'passed', 'system_executables': 'passed',
            'runtime': 'not tested'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--images', type=Path, required=True)
    parser.add_argument('--gerda', type=Path, required=True)
    parser.add_argument('--kernel-build', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(validate(args.images, args.gerda, args.kernel_build), indent=2))
