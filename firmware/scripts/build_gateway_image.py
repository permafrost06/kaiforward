#!/usr/bin/env python3
"""Assemble an experimental Gerda gateway package on the PC; never deploy it."""
import argparse
import gzip
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import zipfile
from validate_modules import validate as validate_modules

PROJECT = Path(__file__).resolve().parents[2]
SYSTEM_SIZE = 838860800
BOOT_SIZE = 26165248


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def run(args, **kwargs):
    return subprocess.run([str(arg) for arg in args], check=True, **kwargs)


def git_revision(path):
    return run(['git', '-C', path, 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()


def normalize_modes(directory):
    # Include the filesystem root itself; rglob('*') only visits descendants.
    # A private host staging directory must not become a root-only phone '/'.
    directory.chmod(0o755)
    for path in directory.rglob('*'):
        if path.is_symlink():
            continue
        path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)


def package_apps(system):
    for app in (system / 'b2g/webapps').iterdir():
        source = app / 'src'
        if not source.is_dir():
            continue
        with zipfile.ZipFile(app / 'application.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(source).as_posix())
        shutil.copyfile(source / 'manifest.webapp', app / 'manifest.webapp')
        shutil.rmtree(source)
    for source_name, archive_name, compression in [
            ('b2g/omni_src', 'b2g/omni.ja', zipfile.ZIP_DEFLATED),
            ('media/bootanimation_src', 'media/bootanimation.zip', zipfile.ZIP_STORED)]:
        source = system / source_name
        if source.exists():
            with zipfile.ZipFile(system / archive_name, 'w', compression) as archive:
                for path in sorted(source.rglob('*')):
                    if path.is_file():
                        archive.write(path, path.relative_to(source).as_posix())
            shutil.rmtree(source)


def kernel_with_original_dtb(new_zimage, original_zimage, output):
    original, new = original_zimage.read_bytes(), new_zimage.read_bytes()
    for name, raw in [('original', original), ('rebuilt', new)]:
        if raw[36:40] != b'\x18\x28\x6f\x01':
            raise ValueError(name + ' kernel is not an ARM zImage')
    original_start, original_end = struct.unpack_from('<2I', original, 40)
    new_start, new_end = struct.unpack_from('<2I', new, 40)
    original_length, new_length = original_end - original_start, new_end - new_start
    dtbs = original[original_length:]
    if not dtbs.startswith(b'\xd0\x0d\xfe\xed'):
        raise ValueError('Expected appended original device-tree blobs')
    if len(new) != new_length:
        raise ValueError('Rebuilt zImage must be plain, without an unreviewed appended DTB')
    offset, count = 0, 0
    while offset < len(dtbs):
        if dtbs[offset:offset + 4] != b'\xd0\x0d\xfe\xed' or offset + 40 > len(dtbs):
            raise ValueError('Malformed appended device-tree sequence')
        size = struct.unpack_from('>I', dtbs, offset + 4)[0]
        if size < 40 or offset + size > len(dtbs):
            raise ValueError('Truncated appended device-tree blob')
        offset += size
        count += 1
    if count != 46:
        raise ValueError('Unexpected Nokia device-tree count')
    output.write_bytes(new + dtbs)
    return {'original_dtb_bytes': len(dtbs), 'original_dtb_count': count,
            'original_dtb_sha256': hashlib.sha256(dtbs).hexdigest()}


def assemble(gerda, kernel_source, kernel_build, wlan_module, output):
    output.mkdir(parents=True, exist_ok=True)
    output.chmod(0o700)
    if any(output.iterdir()):
        raise ValueError('Output directory must be empty; preserve previous builds')
    expected = PROJECT / 'firmware/kernel/kaiforward_incall_playback.h'
    installed = kernel_source / 'sound/soc/msm/qdsp6v2/kaiforward_incall_playback.h'
    if installed.read_bytes() != expected.read_bytes():
        raise ValueError('Kernel source does not contain the reviewed replacement')
    if b'#include "kaiforward_incall_playback.h"' not in (kernel_source / 'sound/soc/msm/qdsp6v2/q6voice.c').read_bytes():
        raise ValueError('Replacement is not included in the kernel source')
    vmlinux = kernel_build / 'vmlinux'
    nm = os.environ.get('GATEWAY_NM', '/home/frost/opt/zephyr-sdk-0.16.9/arm-zephyr-eabi/bin/arm-zephyr-eabi-nm')
    symbols = run([nm, vmlinux], capture_output=True, text=True).stdout
    if ' voc_start_playback\n' not in symbols:
        raise ValueError('Built kernel is missing the playback function')
    if b'kaiforward: incall playback' not in vmlinux.read_bytes():
        raise ValueError('Built kernel lacks the corrected playback diagnostics')

    manifest = {
        'schema': 1, 'device': 'Nokia 8110 4G TA-1059', 'serial': '22aaa475',
        'status': 'experimental; static validation required; runtime unverified',
        'partition_scope': ['boot', 'system'],
        'preserve_partitions': ['modem', 'fsg', 'modemst1', 'modemst2', 'rpm', 'recovery', 'userdata'],
        'gerda_revision': git_revision(gerda), 'kernel_source_revision': git_revision(kernel_source),
        'wlan_revision': git_revision(kernel_source / 'drivers/net/wireless/prima'),
        'kernel_vmlinux_sha256': digest(vmlinux),
        'kernel_zimage_sha256': digest(kernel_build / 'arch/arm/boot/zImage'),
        'kernel_build_banner': re.search(rb'Linux version [^\x00\n]+', vmlinux.read_bytes())[0].decode(),
        'playback_replacement_sha256': digest(expected),
        'kernel_config_sha256': digest(kernel_build / '.config'),
        'kernel_release': (kernel_build / 'include/config/kernel.release').read_text().strip(),
        'health_bootstrap_only': True,
        'limitations': ['No hardware boot or call test yet', 'VoiceMMode correction is unproven on the phone',
                        'Consistent offline userdata backup required before installation',
                        'Gecko SMS/call/USSD control agent and WebRTC are separate implementation stages'],
    }
    with tempfile.TemporaryDirectory(prefix='gateway-image-', dir=PROJECT / 'data/gateway-build') as temporary:
        work = Path(temporary)
        system, ramdisk = work / 'system', work / 'ramdisk'
        shutil.copytree(gerda / 'src/system', system, symlinks=True)
        shutil.copytree(gerda / 'src/boot/8110/root', ramdisk, symlinks=True)
        normalize_modes(system)
        normalize_modes(ramdisk)
        for directory in ['data', 'dev', 'oem', 'proc', 'sys', 'system']:
            (ramdisk / directory).mkdir(mode=0o755, exist_ok=True)
            (ramdisk / directory).chmod(0o755)
        package_apps(system)
        for path in [system / 'b2g/b2g', system / 'kaios/api-daemon']:
            path.chmod(0o755)
        for directory in [system / 'vendor/bin', system / 'vendor/picolisp/bin']:
            for path in directory.iterdir():
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o755)

        # Every loadable module must match the rebuilt kernel, including WLAN.
        modules = sorted(kernel_build.rglob('*.ko'))
        names = [path.name for path in modules]
        if len(names) != len(set(names)):
            raise ValueError('Duplicate module basenames require explicit layout review')
        # The shipped post-boot script explicitly skips core_ctl for Nokia 8110.
        # Its proprietary module has no source here; never load an ABI-mismatched copy.
        required = {'mcDrvModule.ko', 'mcKernelApi.ko'}
        if not required.issubset(set(names)):
            raise ValueError('Rebuilt critical modules missing: ' + str(sorted(required - set(names))))
        if not wlan_module.is_file():
            raise ValueError('Rebuilt WLAN module is required')
        manifest['module_validation'] = validate_modules(kernel_build, modules + [wlan_module])
        modules_directory = system / 'lib/modules'
        for previous in modules_directory.rglob('*.ko'):
            previous.unlink()
        module_manifest = {}
        objcopy = os.environ.get('GATEWAY_OBJCOPY', str(Path(nm).with_name('arm-zephyr-eabi-objcopy')))
        for path in modules:
            destination = modules_directory / path.name
            shutil.copyfile(path, destination)
            run([objcopy, '--strip-debug', destination])
            destination.chmod(0o644)
            module_manifest[path.name] = digest(destination)
        # init.qcom.wifi.sh recreates this link, so retain its expected target.
        wlan_destination = modules_directory / 'pronto/pronto_wlan.ko'
        wlan_destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(wlan_module, wlan_destination)
        run([objcopy, '--strip-debug', wlan_destination])
        wlan_destination.chmod(0o644)
        (modules_directory / 'wlan.ko').symlink_to('/system/lib/modules/pronto/pronto_wlan.ko')
        module_manifest['pronto/pronto_wlan.ko'] = digest(wlan_destination)
        validate_modules(kernel_build, [modules_directory / name for name in module_manifest])
        manifest['modules'] = module_manifest
        manifest['omitted_upstream_modules'] = {
            'core_ctl.ko': 'Not loaded for Nokia 8110 by init.qcom.post_boot.sh; unavailable source'}

        installed_probes = system / 'kaiforward'
        installed_probes.mkdir(mode=0o755)
        shutil.copyfile(PROJECT / 'firmware/ramdisk/health.sh', installed_probes / 'health.sh')
        for name in ['capture_call_audio.sh', 'inject_call_audio.sh']:
            text = (PROJECT / 'tools' / name).read_text().replace(
                '/data/local/tmp/kaiforward-audio-probe', '/data/kaiforward/audio-probe').replace(
                '/data/local/tmp/kaiforward-injection-test.wav', '/system/kaiforward/injection-test.wav')
            (installed_probes / name).write_text(text)
        shutil.copyfile(PROJECT / 'data/audio-probe/injection-test.wav', installed_probes / 'injection-test.wav')
        for path in installed_probes.iterdir():
            path.chmod(0o644)
        shutil.copyfile(PROJECT / 'firmware/ramdisk/init.kaiforward.rc', ramdisk / 'init.kaiforward.rc')
        startup = ramdisk / 'init.b2g.rc'
        startup.write_text('import /init.kaiforward.rc\n' + startup.read_text())
        (ramdisk / 'init.kaiforward.rc').chmod(0o644)

        combined = work / 'zImage-dtb'
        manifest.update(kernel_with_original_dtb(kernel_build / 'arch/arm/boot/zImage',
                                                 gerda / 'src/boot/8110/zImage', combined))
        names = ['.'] + sorted('./' + str(path.relative_to(ramdisk)) for path in ramdisk.rglob('*'))
        cpio = run(['cpio', '--null', '-o', '-H', 'newc', '-R', '0:0'], cwd=ramdisk,
                   input=('\0'.join(names) + '\0').encode(), capture_output=True).stdout
        compressed = work / 'ramdisk.img'
        compressed.write_bytes(gzip.compress(cpio, compresslevel=9, mtime=0))
        boot = output / 'boot.img'
        run([gerda / 'tools/abootimg/abootimg', '--create', boot, '-f',
             gerda / 'src/boot/8110/bootimg.cfg', '-k', combined, '-r', compressed])
        if boot.stat().st_size != BOOT_SIZE:
            raise ValueError('Boot image size differs from the verified phone partition')
        image = output / 'system.img'
        run([gerda / 'tools/make_ext4fs/make_ext4fs', '-a', '/system', '-L', 'system',
             '-j', '0', '-l', str(SYSTEM_SIZE), image, system])
        if image.stat().st_size != SYSTEM_SIZE:
            raise ValueError('System image size differs from the verified phone partition')
        # Android's default fs_config overrides chmod for programs outside bin/.
        # Change only execute modes in this new PC image, retaining owners/caps.
        executables = ['b2g/b2g', 'b2g/plugin-container', 'kaios/api-daemon',
                       'vendor/picolisp/bin/picolisp']
        for path in executables:
            run(['debugfs', '-w', '-R', 'set_inode_field /' + path + ' mode 0100755', image],
                capture_output=True)
        manifest['explicit_executable_permissions'] = executables
        # This old make_ext4fs leaves padding bits clear in the last inode bitmap.
        # Normalize only the new PC image, then require a clean read-only check.
        check = subprocess.run(['e2fsck', '-fy', str(image)], capture_output=True, text=True)
        (output / 'filesystem-build-check.txt').write_text(check.stdout + check.stderr)
        if check.returncode not in (0, 1):
            raise ValueError('New filesystem repair failed; no deployable image produced')
        run(['e2fsck', '-fn', image], capture_output=True)

    manifest['images'] = {name: {'bytes': (output / name).stat().st_size, 'sha256': digest(output / name)}
                          for name in ['boot.img', 'system.img']}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'SHA256SUMS').write_text(''.join(details['sha256'] + '  ' + name + '\n'
                                             for name, details in manifest['images'].items()))
    return manifest


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--gerda', type=Path, required=True)
    parser.add_argument('--kernel-source', type=Path, required=True)
    parser.add_argument('--kernel-build', type=Path, required=True)
    parser.add_argument('--wlan-module', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = assemble(args.gerda.resolve(), args.kernel_source.resolve(), args.kernel_build.resolve(),
                      args.wlan_module.resolve(), args.output.resolve())
    print(json.dumps(result, indent=2))
