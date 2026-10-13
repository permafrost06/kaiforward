#!/usr/bin/env python3
"""Verify Android recovery's whole-file signature with the expected certificate."""
import argparse
from pathlib import Path
import struct
import subprocess
import tempfile


def verify(path, certificate):
    with path.open('rb') as archive:
        archive.seek(-6, 2)
        signature_start, marker, comment_size = struct.unpack('<3H', archive.read(6))
        if marker != 0xffff or signature_start > comment_size or signature_start <= 6:
            raise ValueError('Malformed Android whole-file signature footer')
        archive.seek(-22 - comment_size, 2)
        if archive.read(4) != b'PK\x05\x06':
            raise ValueError('Missing ZIP end-of-central-directory record')
        archive.seek(-signature_start, 2)
        signature = archive.read(signature_start - 6)
        signed_bytes = path.stat().st_size - comment_size - 2
        with tempfile.TemporaryDirectory(dir=path.parent, prefix='signature-check-') as temporary:
            root = Path(temporary)
            (root / 'signature.der').write_bytes(signature)
            archive.seek(0)
            with (root / 'content.bin').open('wb') as content:
                remaining = signed_bytes
                while remaining:
                    chunk = archive.read(min(remaining, 1048576))
                    if not chunk:
                        raise ValueError('Truncated signed package')
                    content.write(chunk)
                    remaining -= len(chunk)
            subprocess.run(['openssl', 'cms', '-verify', '-inform', 'DER', '-binary',
                            '-in', str(root / 'signature.der'), '-content', str(root / 'content.bin'),
                            '-nointern', '-certfile', str(certificate), '-noverify',
                            '-out', '/dev/null'], check=True)
    return True


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--certificate', required=True, type=Path)
    parser.add_argument('packages', type=Path, nargs='+')
    args = parser.parse_args()
    for path in args.packages:
        verify(path, args.certificate)
        print('Verified whole-file signature: ' + str(path))
