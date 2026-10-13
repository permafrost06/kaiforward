#!/usr/bin/env python3
"""Build the app with the local receiver's credentials; no secret is printed."""
import json
from pathlib import Path
import struct
import sys
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
from receiver import load_config


def icon():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    # Simple code-generated app icon, with no external visual dependency.
    pixels = bytearray()
    for y in range(112):
        pixels.append(0)
        for x in range(112):
            pixels.extend((112, 224, 186) if 24 <= x < 88 and 32 <= y < 80 else (16, 29, 40))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 112, 112, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b'')


def main():
    config = load_config(ROOT / 'data')
    config['endpoint'] = 'http://127.0.0.1:8765'
    destination = ROOT / 'dist' / 'kaiforward.zip'
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in (ROOT / 'phone').iterdir():
            if path.is_file():
                archive.write(path, path.name)
        archive.writestr('config.js', 'window.KAIFORWARD_CONFIG = ' + json.dumps(config) + ';\n')
        archive.writestr('icon.png', icon())
    destination.chmod(0o600)
    print('Built ' + str(destination))


if __name__ == '__main__':
    main()
