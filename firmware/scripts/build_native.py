#!/usr/bin/env python3
"""Build a static ARM/Linux native service. Never connects to a phone."""
import argparse
import hashlib
import json
from pathlib import Path
from scripts.files import file_sha256
import shutil
import subprocess
import tarfile
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parents[2]
NATIVE = PROJECT / 'firmware/native'


def prepare(root):
    root.mkdir(parents=True, exist_ok=True)
    inputs = json.loads((NATIVE / 'inputs.json').read_text())
    for name, entry in inputs.items():
        archive = root / ('zig.tar.xz' if name == 'zig' else 'sqlite.zip')
        if not archive.exists():
            partial = archive.with_suffix('.partial')
            urllib.request.urlretrieve(entry['url'], partial)
            partial.rename(archive)
        if file_sha256(archive) != entry['sha256']:
            raise ValueError('Build input checksum mismatch: ' + name)
        if name == 'zig':
            with tarfile.open(archive) as bundle:
                bundle.extractall(root, filter='data')
        else:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(root)
    return root / 'zig-aarch64-linux-0.15.2/zig', root / 'sqlite-amalgamation-3500400'


def build(root, output, zig=None, sqlite=None):
    if zig is None or sqlite is None:
        prepared_zig, prepared_sqlite = prepare(root)
        zig, sqlite = zig or prepared_zig, sqlite or prepared_sqlite
    output.mkdir(parents=True, exist_ok=True)
    binary = output / 'kaiforward-native'
    sources = sorted(NATIVE.glob('*.c'))
    command = [str(zig), 'cc', '-target', 'arm-linux-musleabi', '-mcpu=cortex_a7',
               '-static', '-std=gnu11', '-Os', '-fno-strict-aliasing',
               '-DSQLITE_THREADSAFE=0', '-DSQLITE_OMIT_LOAD_EXTENSION',
               '-DSQLITE_DEFAULT_WAL_SYNCHRONOUS=2', '-I' + str(sqlite),
               *map(str, sources), str(sqlite / 'sqlite3.c'), '-o', str(binary)]
    subprocess.run(command, check=True)
    # No interpreter/Bionic dependency; raw Linux syscalls support the 3.10 kernel.
    raw = binary.read_bytes()
    if raw[:7] != b'\x7fELF\x01\x01\x01' or int.from_bytes(raw[18:20], 'little') != 40:
        raise ValueError('Native output is not 32-bit little-endian ARM ELF')
    binary.chmod(0o755)
    for name in ['init.native.rc', 'wifi.sh', 'tailscale.sh', 'ssh.sh', 'udhcpc.sh', 'dhcp-native.conf', 'FONT-LICENSE.txt']:
        shutil.copyfile(NATIVE / name, output / name)
    manifest = {'schema': 1, 'status': 'experimental; hardware validation pending',
                'target': 'arm-linux-musleabi cortex_a7, static',
                'inputs': json.loads((NATIVE / 'inputs.json').read_text()),
                'sources': {str(p.relative_to(PROJECT)): file_sha256(p)
                            for p in sorted(NATIVE.iterdir()) if p.is_file()},
                'binary_sha256': hashlib.sha256(raw).hexdigest(), 'binary_bytes': len(raw)}
    (output / 'native-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-root', type=Path, default=PROJECT / 'data/native-build')
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist/native-runtime')
    parser.add_argument('--zig', type=Path)
    parser.add_argument('--sqlite', type=Path)
    args = parser.parse_args()
    print(json.dumps(build(args.build_root.resolve(), args.output.resolve(), args.zig, args.sqlite), indent=2))
