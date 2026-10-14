"""Build a pinned, static ARM Dropbear server and host-key generator."""
import argparse
import json
from pathlib import Path
import shutil
import shlex
import subprocess
import tarfile
import urllib.request
from scripts.files import file_sha256

PROJECT = Path(__file__).resolve().parents[2]
VERSION = '2026.94'
SHA256 = 'e098034a843699200c8c977a991fff73159735bf795d5f72ef672c41a6b1ae81'
URL = f'https://matt.ucc.asn.au/dropbear/releases/dropbear-{VERSION}.tar.bz2'


def build(cache, output, zig):
    if not zig.is_file():
        from firmware.scripts.build_native import prepare
        zig, _ = prepare(PROJECT / 'data/native-build')
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / f'dropbear-{VERSION}.tar.bz2'
    if not archive.exists():
        partial = archive.with_suffix('.partial')
        try:
            request = urllib.request.Request(URL, headers={'User-Agent': 'KaiForward/1.0'})
            with urllib.request.urlopen(request, timeout=30) as response, partial.open('wb') as dest:
                shutil.copyfileobj(response, dest)
            if file_sha256(partial) != SHA256:
                raise ValueError('Downloaded Dropbear source checksum mismatch')
            partial.replace(archive)
        finally:
            partial.unlink(missing_ok=True)
    if file_sha256(archive) != SHA256:
        raise ValueError('Dropbear source checksum mismatch')
    with tarfile.open(archive) as bundle:
        bundle.extractall(cache, filter='data')
    source = cache / f'dropbear-{VERSION}'
    shutil.copyfile(PROJECT / 'firmware/native/ssh-options.h', source / 'localoptions.h')
    compiler = source / 'arm-cc'
    compiler.write_text('#!/bin/sh\nexec ' + shlex.quote(str(zig.resolve())) +
                        ' cc -target arm-linux-musleabi -mcpu=cortex_a7 "$@"\n')
    compiler.chmod(0o755)
    flags = '-Os'
    subprocess.run(['./configure', '--host=arm-linux-musleabi', '--disable-zlib',
                    '--disable-lastlog', '--disable-utmp', '--disable-utmpx',
                    '--disable-wtmp', '--disable-wtmpx', '--disable-syslog',
                    'CC=' + str(compiler.resolve()), 'CFLAGS=' + flags], cwd=source, check=True)
    shutil.copyfile(PROJECT / 'firmware/native/ssh-account.h', source / 'src/ssh-account.h')
    includes = source / 'src/includes.h'
    includes.write_text(includes.read_text() + '\n#include "ssh-account.h"\n')
    subprocess.run(['make', '-j4', 'PROGRAMS=dropbear dropbearkey', 'STATIC=1'], cwd=source, check=True)
    output.mkdir(parents=True, exist_ok=True)
    for name in ('dropbear', 'dropbearkey'):
        shutil.copyfile(source / name, output / name)
        (output / name).chmod(0o700)
    shutil.copyfile(source / 'LICENSE', output / 'DROPBEAR-LICENSE')
    (output / 'ssh-manifest.json').write_text(json.dumps({
        'version': VERSION, 'url': URL, 'source_sha256': SHA256,
        'files': {p.name: file_sha256(p) for p in output.iterdir() if p.is_file() and p.name != 'ssh-manifest.json'},
        'configuration': {name: file_sha256(PROJECT / 'firmware/native' / name)
                          for name in ('ssh-account.h', 'ssh-options.h')}
    }, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cache', type=Path, default=PROJECT / 'data/ssh-build')
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist/ssh-runtime')
    parser.add_argument('--zig', type=Path, default=PROJECT / 'data/native-build/zig-aarch64-linux-0.15.2/zig')
    args = parser.parse_args()
    build(args.cache.resolve(), args.output.resolve(), args.zig)
