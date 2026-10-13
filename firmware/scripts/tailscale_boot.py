"""Add the Tailscale service to a Nokia native boot image without replacing its kernel."""
import gzip
import hashlib
import re
import struct

from .cpio import cpio_files
from .image_utils import BOOT_SIZE

INIT = 'init.kaiforward-native.rc'
SERVICE = b'''service kf-tailscale /system/bin/sh /system/kaiforward/tailscale.sh
    class main
    user root
    group root inet net_raw net_admin
'''


def startup_with_tailscale(content):
    """Preserve existing startup definitions; reject a conflicting service."""
    pattern = rb'(?m)^service kf-tailscale[^\n]*\n(?:[ \t]+[^\n]*\n)*'
    matches = list(re.finditer(pattern, content))
    if matches:
        if len(matches) != 1 or matches[0].group() != SERVICE:
            raise ValueError('Existing kf-tailscale service differs from the supported definition')
        return content
    return content.rstrip(b'\n') + b'\n\n' + SERVICE


def replace_entry(archive, name, replacement):
    """Retain newc entry metadata and the raw bytes of every other entry."""
    parts, offset, count = [], 0, 0
    while archive[offset:offset + 6] == b'070701':
        header = archive[offset:offset + 110]
        if len(header) != 110:
            raise ValueError('Truncated cpio header')
        fields = [int(header[6 + 8 * i:14 + 8 * i], 16) for i in range(13)]
        name_end = offset + 110 + fields[11]
        if fields[11] < 1 or name_end > len(archive) or archive[name_end - 1] != 0:
            raise ValueError('Invalid cpio filename')
        entry_name = archive[offset + 110:name_end - 1].decode()
        start = (name_end + 3) & ~3
        end = (start + fields[6] + 3) & ~3
        if end > len(archive):
            raise ValueError('Truncated cpio payload')
        if entry_name.removeprefix('./') == name:
            fields[6] = len(replacement)
            updated = b'070701' + b''.join(f'{value:08x}'.encode() for value in fields)
            entry = updated + archive[offset + 110:start] + replacement
            parts.append(entry + b'\0' * (-len(entry) % 4))
            count += 1
        else:
            parts.append(archive[offset:end])
        offset = end
        if entry_name == 'TRAILER!!!':
            if count != 1:
                raise ValueError('Expected exactly one native init entry')
            result = b''.join(parts)
            return result + b'\0' * (-len(result) % 512)
    raise ValueError('Malformed cpio archive')


def patch_boot(raw):
    """Return (image, manifest), or the original bytes when already configured."""
    if len(raw) != BOOT_SIZE or raw[:8] != b'ANDROID!':
        raise ValueError('Expected a full Nokia 8110 boot partition image')
    kernel_size, kernel_addr, ramdisk_size, ramdisk_addr, second, _, tags, page, dt, unused = struct.unpack_from('<10I', raw, 8)
    if (kernel_addr, ramdisk_addr, tags, page, second, dt, unused) != (
            0x80008000, 0x81000000, 0x80000100, 2048, 0, 0, 0):
        raise ValueError('Unsupported boot layout; refusing to discard extra payloads')
    align = lambda size: (size + page - 1) // page * page
    offset = page + align(kernel_size)
    if not kernel_size or not ramdisk_size or offset + align(ramdisk_size) > len(raw):
        raise ValueError('Invalid boot payload sizes')
    if any(raw[offset + align(ramdisk_size):]):
        raise ValueError('Unexpected trailing boot payload or signature')
    ramdisk = raw[offset:offset + ramdisk_size]
    archive = gzip.decompress(ramdisk)
    original = cpio_files(archive)
    if INIT not in original or 'init.b2g.rc' not in original or not re.search(
            rb'(?m)^import /init\.kaiforward-native\.rc\s*$', original['init.b2g.rc'][1]):
        raise ValueError('Boot image is not the supported native KaiForward firmware')
    updated = startup_with_tailscale(original[INIT][1])
    kernel = raw[page:page + kernel_size]
    manifest = {'original_sha256': hashlib.sha256(raw).hexdigest(),
                'bytes': len(raw), 'kernel_sha256': hashlib.sha256(kernel).hexdigest(),
                'changed': updated != original[INIT][1]}
    if updated == original[INIT][1]:
        manifest['patched_sha256'] = manifest['original_sha256']
        return raw, manifest
    changed_archive = replace_entry(archive, INIT, updated)
    modified = cpio_files(changed_archive)
    expected = dict(original, **{INIT: (original[INIT][0], updated)})
    if modified != expected:
        raise ValueError('Ramdisk validation failed: an unrelated entry changed')

    def boot_id(payload):
        digest = hashlib.sha1()
        for content in (kernel, payload, b''):
            digest.update(content)
            digest.update(struct.pack('<I', len(content)))
        return digest.digest()

    if any(raw[576:608]) and raw[576:608] != boot_id(ramdisk) + b'\0' * 12:
        raise ValueError('Unsupported original boot ID')
    compressed = gzip.compress(changed_archive, compresslevel=9, mtime=0)
    header = bytearray(raw[:page])
    struct.pack_into('<I', header, 16, len(compressed))
    # abootimg-generated images have a zero ID; retain that convention.
    if any(raw[576:608]):
        header[576:608] = boot_id(compressed) + b'\0' * 12
    result = bytes(header) + raw[page:offset] + compressed + b'\0' * (align(len(compressed)) - len(compressed))
    if len(result) > len(raw):
        raise ValueError('Patched ramdisk exceeds the boot partition')
    result += b'\0' * (len(raw) - len(result))
    if result[page:page + kernel_size] != kernel or cpio_files(
            gzip.decompress(result[offset:offset + len(compressed)])) != expected:
        raise ValueError('Patched boot image failed validation')
    manifest['patched_sha256'] = hashlib.sha256(result).hexdigest()
    return result, manifest
