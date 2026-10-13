#!/usr/bin/env python3
"""Shared PC-side image assembly helpers for the Nokia 8110."""
import hashlib
import shutil
import struct
import subprocess
import zipfile

SYSTEM_SIZE = 838860800
BOOT_SIZE = 26165248


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
