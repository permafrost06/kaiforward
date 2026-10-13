#!/usr/bin/env python3
"""Reject ARM modules whose release or symbol versions differ from the build."""
import argparse
import json
from pathlib import Path
import struct


def sections(path):
    raw = path.read_bytes()
    if raw[:6] != b'\x7fELF\x01\x01' or struct.unpack_from('<H', raw, 18)[0] != 40:
        raise ValueError('Expected a little-endian ELF32 ARM module: ' + str(path))
    offset = struct.unpack_from('<I', raw, 32)[0]
    size, count, strings_index = struct.unpack_from('<3H', raw, 46)
    headers = [struct.unpack_from('<10I', raw, offset + size * index) for index in range(count)]
    strings = headers[strings_index]
    names = raw[strings[4]:strings[4] + strings[5]]
    return {names[item[0]:].split(b'\0', 1)[0].decode(): raw[item[4]:item[4] + item[5]]
            for item in headers}


def validate(build, modules):
    release = (build / 'include/config/kernel.release').read_text().strip()
    versions = {}
    for line in (build / 'Module.symvers').read_text().splitlines():
        fields = line.split()
        versions[fields[1]] = int(fields[0], 16)
    results = {}
    for path in modules:
        data = sections(path)
        info = dict(item.decode().split('=', 1) for item in data['.modinfo'].split(b'\0') if b'=' in item)
        if info['vermagic'].split()[0] != release:
            raise ValueError('Kernel release mismatch: ' + str(path))
        entries = data.get('__versions', b'')
        if not entries or len(entries) % 64:
            raise ValueError('Missing or malformed module symbol versions: ' + str(path))
        checked = []
        for offset in range(0, len(entries), 64):
            crc = struct.unpack_from('<I', entries, offset)[0]
            name = entries[offset + 4:offset + 64].split(b'\0', 1)[0].decode()
            if versions.get(name) != crc:
                raise ValueError('Symbol CRC mismatch for ' + name + ' in ' + str(path))
            checked.append(name)
        if 'module_layout' not in checked:
            raise ValueError('Module layout version is absent: ' + str(path))
        results[str(path)] = {'vermagic': info['vermagic'], 'checked_symbols': len(checked)}
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', type=Path, required=True)
    parser.add_argument('modules', type=Path, nargs='+')
    args = parser.parse_args()
    print(json.dumps(validate(args.build, args.modules), indent=2))
