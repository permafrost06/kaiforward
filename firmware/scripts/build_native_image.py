#!/usr/bin/env python3
"""Assemble native Gerda UI/SMS/call images with the validated call kernel."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
from scripts.files import file_sha256 as digest
import re
import shutil
import subprocess
import tempfile

from .image_utils import BOOT_SIZE, SYSTEM_SIZE, normalize_modes, package_apps, run, git_revision
from .cpio import cpio_files

PROJECT = Path(__file__).resolve().parents[2]
NATIVE = PROJECT / 'firmware/native'
DISABLED = ('b2g', 'rilproxy', 'api-daemon')


def native_startup(text):
    for name in DISABLED:
        pattern = r'(?m)^(service ' + re.escape(name) + r' [^\n]+\n)'
        text, count = re.subn(pattern, r'\1    disabled\n', text)
        if count != 1:
            raise ValueError('Unexpected upstream service definition: ' + name)
    return 'import /init.kaiforward-native.rc\n' + text


def validate(output):
    manifest = json.loads((output / 'manifest.json').read_text())
    for name, expected in manifest['images'].items():
        if (output / name).stat().st_size != expected['bytes'] or digest(output / name) != expected['sha256']:
            raise ValueError('Image size/hash mismatch: ' + name)
    raw = (output / 'boot.img').read_bytes()
    import struct
    if raw[:8] != b'ANDROID!':
        raise ValueError('Boot header missing')
    kernel_size, address, ramdisk_size, ramdisk_address, second, _, tags, page = struct.unpack_from('<8I', raw, 8)
    if (address, ramdisk_address, tags, page, second) != (0x80008000, 0x81000000, 0x80000100, 2048, 0):
        raise ValueError('Unexpected Nokia boot layout')
    expected_kernel = manifest.get('kernel_payload_sha256', manifest['upstream_kernel_sha256'])
    if hashlib.sha256(raw[page:page + kernel_size]).hexdigest() != expected_kernel:
        raise ValueError('Validated kernel/DTB payload differs from manifest')
    offset = page + ((kernel_size + page - 1) // page) * page
    files = cpio_files(gzip.decompress(raw[offset:offset + ramdisk_size]))
    for name in ['.', 'data', 'dev', 'oem', 'proc', 'sys', 'system']:
        if files[name][0] & 0o777 != 0o755:
            raise ValueError('Ramdisk root/mountpoint traversal permissions missing: ' + name)
    for name in re.findall(r'(?m)^service\s+(\S+)', files['init.kaiforward-native.rc'][1].decode()):
        if len(name) > 16:
            raise ValueError('Legacy init service name exceeds 16 characters: ' + name)
    startup = files['init.b2g.rc'][1].decode()
    for name in DISABLED:
        if not re.search(r'(?m)^service ' + name + r' [^\n]+\n    disabled\n', startup):
            raise ValueError('Browser service still enabled: ' + name)
    if 'import /init.kaiforward-native.rc\n' not in startup or files['init.kaiforward-native.rc'][1] != (NATIVE / 'init.native.rc').read_bytes():
        raise ValueError('Native startup not imported / definition changed')
    run(['e2fsck', '-fn', output / 'system.img'], capture_output=True)
    for path, expected in manifest['native_files'].items():
        content = run(['debugfs', '-R', 'cat /' + path, output / 'system.img'], capture_output=True).stdout
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError('Native file content mismatch: ' + path)
    for path in manifest['executables']:
        listing = run(['debugfs', '-R', 'stat /' + path, output / 'system.img'], capture_output=True, text=True).stdout
        mode = re.search(r'Mode:\s+([0-7]+)', listing)
        if not mode or not int(mode[1], 8) & 0o111:
            raise ValueError('Executable permission missing: ' + path)
    return {'boot_layout': 'passed', 'validated_kernel_and_original_dtbs': 'passed',
            'browser_and_radio_proxy_disabled': 'passed', 'native_service_startup': 'passed',
            'native_payload_hashes': 'passed', 'ext4_read_only_check': 'passed',
            'executable_modes': 'passed', 'hardware_boot_wifi_sms_ui': 'not tested'}


def assemble(gerda, runtime, output, ca_bundle=None, call_kernel=None, relay_runtime=None):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output must be empty; preserve previous firmware')
    pinned = json.loads((PROJECT / 'firmware/sources.json').read_text())['gerda']
    if git_revision(gerda) != pinned:
        raise ValueError('Unexpected Gerda source revision')
    runtime_manifest = json.loads((runtime / 'native-manifest.json').read_text())
    if digest(runtime / 'kaiforward-native') != runtime_manifest['binary_sha256']:
        raise ValueError('Native binary does not match its manifest')
    for path, expected in runtime_manifest['sources'].items():
        if digest(PROJECT / path) != expected:
            raise ValueError('Native sources changed after build: ' + path)
    call_kernel = call_kernel or PROJECT / 'dist/native-call-kernel'
    relay_runtime = relay_runtime or PROJECT / 'dist/native-call-relay'
    call_manifest = json.loads((call_kernel / 'manifest.json').read_text())
    relay_manifest = json.loads((relay_runtime / 'relay-manifest.json').read_text())
    if call_manifest['source_revisions'] != json.loads((PROJECT / 'firmware/sources.json').read_text()):
        raise ValueError('Prepared call kernel uses different source revisions')
    if call_manifest['playback_replacement_sha256'] != digest(PROJECT / 'firmware/kernel/kaiforward_incall_playback.h'):
        raise ValueError('Kernel patch changed after kernel preparation')
    if digest(call_kernel / 'zImage-dtb') != call_manifest['kernel_payload_sha256']:
        raise ValueError('Prepared call kernel does not match its manifest')
    if digest(relay_runtime / 'kaiforward-relay') != relay_manifest['binary_sha256']:
        raise ValueError('Native call helper differs from its manifest')
    for path, expected in relay_manifest['sources'].items():
        if digest(PROJECT / path) != expected:
            raise ValueError('Call helper sources changed after build: ' + path)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if ca_bundle is None:
        ca_bundle = next((p for p in [Path('/etc/pki/tls/certs/ca-bundle.crt'),
                                     Path('/etc/ssl/certs/ca-certificates.crt')] if p.exists()), None)
    if ca_bundle is None or b'BEGIN CERTIFICATE' not in ca_bundle.read_bytes():
        raise ValueError('Provide a public CA bundle for HTTPS forwarding')
    manifest = {'schema': 1, 'device': 'Nokia 8110 4G TA-1059',
                'status': 'experimental; PC validation only; hardware boot unverified',
                'partition_scope': ['boot', 'system'], 'gerda_revision': pinned,
                'upstream_kernel_sha256': digest(gerda / 'src/boot/8110/zImage'),
                'kernel_changed': True, 'native_runtime': runtime_manifest,
                'call_kernel': call_manifest, 'relay_runtime': relay_manifest,
                'configuration': '/data/kaiforward/config', 'credentials_in_image': False,
                'disabled_services': list(DISABLED)}
    staging = PROJECT / 'data/native-build'
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='native-image-', dir=staging) as tmp:
        work = Path(tmp)
        system, ramdisk = work / 'system', work / 'ramdisk'
        shutil.copytree(gerda / 'src/system', system, symlinks=True)
        shutil.copytree(gerda / 'src/boot/8110/root', ramdisk, symlinks=True)
        normalize_modes(system)
        normalize_modes(ramdisk)
        package_apps(system)
        for previous in list((system / 'lib/modules').rglob('*.ko')):
            previous.unlink()
        for name, expected in call_manifest['modules'].items():
            source = call_kernel / 'modules' / name
            if digest(source) != expected:
                raise ValueError('Prepared call module differs: ' + name)
            destination = system / 'lib/modules' / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
        (system / 'lib/modules/wlan.ko').symlink_to('/system/lib/modules/pronto/pronto_wlan.ko')
        for directory in ['data', 'dev', 'oem', 'proc', 'sys', 'system']:
            (ramdisk / directory).mkdir(mode=0o755, exist_ok=True)
            (ramdisk / directory).chmod(0o755)
        payload = system / 'kaiforward'
        payload.mkdir(exist_ok=True)
        shutil.copyfile(runtime / 'kaiforward-native', system / 'bin/kaiforward-native')
        shutil.copyfile(relay_runtime / 'kaiforward-relay', system / 'bin/kaiforward-relay')
        shutil.copyfile(NATIVE / 'wifi.sh', payload / 'wifi.sh')
        shutil.copyfile(NATIVE / 'udhcpc.sh', payload / 'udhcpc.sh')
        shutil.copyfile(NATIVE / 'FONT-LICENSE.txt', payload / 'FONT-LICENSE.txt')
        shutil.copyfile(ca_bundle, payload / 'ca.pem')
        shutil.copyfile(NATIVE / 'dhcp-native.conf', system / 'etc/dhcpcd/dhcpcd-hooks/99-kaiforward.conf')
        shutil.copyfile(NATIVE / 'init.native.rc', ramdisk / 'init.kaiforward-native.rc')
        startup = ramdisk / 'init.b2g.rc'
        startup.write_text(native_startup(startup.read_text()))
        manifest['native_files'] = {path: digest(system / path) for path in [
            'bin/kaiforward-native', 'bin/kaiforward-relay', 'kaiforward/wifi.sh', 'kaiforward/udhcpc.sh', 'kaiforward/FONT-LICENSE.txt', 'kaiforward/ca.pem',
            'etc/dhcpcd/dhcpcd-hooks/99-kaiforward.conf']}
        manifest['native_files'].update({'lib/modules/' + name: expected for name, expected in call_manifest['modules'].items()})
        executables = ['bin/surfaceflinger', 'bin/mediaserver', 'kaiforward/udhcpc.sh', 'bin/kaiforward-native', 'bin/kaiforward-relay', 'bin/curl', 'bin/wpa_supplicant', 'bin/dhcpcd',
                       'bin/ndc', 'bin/rild', 'bin/busybox', 'bin/sh',
                       'etc/dhcpcd/dhcpcd-run-hooks', 'vendor/picolisp/bin/picolisp']
        for directory in ['vendor/bin', 'vendor/picolisp/bin']:
            for path in (system / directory).iterdir():
                if path.is_file() and not path.is_symlink():
                    executables.append(path.relative_to(system).as_posix())
        for path in executables:
            (system / path).chmod(0o755)
        names = ['.'] + sorted('./' + str(path.relative_to(ramdisk)) for path in ramdisk.rglob('*'))
        cpio = run(['cpio', '--null', '-o', '-H', 'newc', '-R', '0:0'], cwd=ramdisk,
                   input=('\0'.join(names) + '\0').encode(), capture_output=True).stdout
        compressed = work / 'ramdisk.img'
        compressed.write_bytes(gzip.compress(cpio, compresslevel=9, mtime=0))
        kernel = call_kernel / 'zImage-dtb'
        manifest['kernel_payload_sha256'] = digest(kernel)
        run([gerda / 'tools/abootimg/abootimg', '--create', output / 'boot.img', '-f',
             gerda / 'src/boot/8110/bootimg.cfg', '-k', kernel, '-r', compressed])
        run([gerda / 'tools/make_ext4fs/make_ext4fs', '-a', '/system', '-L', 'system',
             '-j', '0', '-l', str(SYSTEM_SIZE), output / 'system.img', system])
        for path in executables:
            run(['debugfs', '-w', '-R', 'set_inode_field /' + path + ' mode 0100755', output / 'system.img'], capture_output=True)
        check = subprocess.run(['e2fsck', '-fy', str(output / 'system.img')], capture_output=True, text=True)
        (output / 'filesystem-build-check.txt').write_text(check.stdout + check.stderr)
        if check.returncode not in (0, 1):
            raise ValueError('New system image filesystem check failed')
    for name, size in [('boot.img', BOOT_SIZE), ('system.img', SYSTEM_SIZE)]:
        if (output / name).stat().st_size != size:
            raise ValueError('Image differs from verified partition size: ' + name)
    manifest['images'] = {name: {'bytes': (output / name).stat().st_size, 'sha256': digest(output / name)} for name in ['boot.img', 'system.img']}
    manifest['executables'] = sorted(set(executables))
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'validation.json').write_text(json.dumps(validate(output), indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(entry['sha256'] + '  ' + name + '\n' for name, entry in manifest['images'].items()))
    return manifest


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gerda', type=Path, default=PROJECT / 'data/gateway-build/gerda')
    parser.add_argument('--runtime', type=Path, default=PROJECT / 'dist/native-runtime')
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist/gerda-native')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--ca-bundle', type=Path)
    parser.add_argument('--call-kernel', type=Path, default=PROJECT / 'dist/native-call-kernel')
    parser.add_argument('--relay-runtime', type=Path, default=PROJECT / 'dist/native-call-relay')
    args = parser.parse_args()
    result = validate(args.output) if args.validate_only else assemble(args.gerda.resolve(), args.runtime.resolve(), args.output.resolve(), args.ca_bundle, args.call_kernel, args.relay_runtime)
    print(json.dumps(result, indent=2))
