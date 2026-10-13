#!/usr/bin/env python3
"""Build the standalone static ARM Wi-Fi call helper with verified TLS inputs."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
TLS_URL = 'https://github.com/Mbed-TLS/mbedtls/releases/download/mbedtls-3.6.7/mbedtls-3.6.7.tar.bz2'
TLS_SHA256 = 'a7e8bcbec0e6f761b4af24f25677626b35f762f68eef79c08677a363212d11f6'


def build(zig, sqlite, work, output, host=False):
    work.mkdir(parents=True, exist_ok=True)
    archive = work / 'mbedtls-3.6.7.tar.bz2'
    if not archive.exists():
        urllib.request.urlretrieve(TLS_URL, archive)
    if hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() != TLS_SHA256:
        raise ValueError('TLS source checksum mismatch')
    tls = work / 'mbedtls-3.6.7'
    if not tls.exists():
        with tarfile.open(archive) as bundle:
            bundle.extractall(work, filter='data')
    output.mkdir(parents=True, exist_ok=True)
    binary = output / ('kaiforward-relay-host' if host else 'kaiforward-relay')
    sources = [ROOT / 'firmware/relay/relay.c', ROOT / 'firmware/relay/pcm.c', ROOT / 'firmware/native/common.c']
    compiler = ['gcc'] if host else [str(zig), 'cc', '-target', 'arm-linux-musleabi', '-mcpu=cortex_a7', '-static']
    subprocess.run([*compiler, '-std=gnu11', '-Os', '-ffunction-sections', '-fdata-sections',
                    '-Wl,--gc-sections', '-I' + str(sqlite), '-I' + str(tls / 'include'),
                    '-I' + str(tls / 'library'), *map(str, sources),
                    *map(str, sorted((tls / 'library').glob('*.c'))), '-o', str(binary)], check=True)
    binary.chmod(0o755)
    report = {'target': 'host' if host else 'static ARM Cortex-A7 Linux',
              'tls': {'url': TLS_URL, 'sha256': TLS_SHA256},
              'binary_sha256': hashlib.file_digest(binary.open('rb'), 'sha256').hexdigest(),
              'binary_bytes': binary.stat().st_size,
              'sources': {str(p.relative_to(ROOT)): hashlib.file_digest(p.open('rb'), 'sha256').hexdigest()
                          for p in sources + [ROOT / 'firmware/relay/pcm.h', ROOT / 'firmware/native/native.h']}}
    (output / ('relay-host-manifest.json' if host else 'relay-manifest.json')).write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--zig', type=Path, default=ROOT / 'data/native-build/zig-aarch64-linux-0.15.2/zig')
    p.add_argument('--sqlite', type=Path, default=ROOT / 'data/native-build/sqlite-amalgamation-3500400')
    p.add_argument('--work', type=Path, default=ROOT / 'data/call-relay')
    p.add_argument('--output', type=Path, default=ROOT / 'dist/native-call-relay')
    p.add_argument('--host', action='store_true')
    a = p.parse_args()
    print(json.dumps(build(a.zig, a.sqlite, a.work, a.output, a.host), indent=2))
