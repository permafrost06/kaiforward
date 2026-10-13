#!/usr/bin/env python3
"""Read a root-owned newc archive from an Android boot ramdisk."""

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
