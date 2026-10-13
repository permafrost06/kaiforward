#!/usr/bin/env python3
"""Fetch pinned build inputs and apply PC-only source changes; never access ADB."""
import argparse
import json
from pathlib import Path
import subprocess

from apply_kernel_patch import apply

PROJECT = Path(__file__).resolve().parents[2]
REPOSITORIES = {
    'gerda': 'https://gitlab.com/project-pris/system.git',
    'kernel': 'https://github.com/HMD-OSS-Archive/android_kernel_nokia_8110.git',
    'wlan': 'https://gitlab.com/affenull2345/prima-wlan.git',
    'toolchain': 'https://android.googlesource.com/platform/prebuilts/gcc/linux-x86/arm/arm-eabi-4.8',
}


def run(*args):
    return subprocess.run([str(arg) for arg in args], check=True, capture_output=True, text=True).stdout.strip()


def prepare(root, stock_config):
    root.mkdir(parents=True, exist_ok=True)
    revisions = json.loads((PROJECT / 'firmware/sources.json').read_text())
    paths = {name: root / name for name in REPOSITORIES}
    paths['wlan'] = root / 'kernel/drivers/net/wireless/prima'
    for name, url in REPOSITORIES.items():
        path = paths[name]
        if not path.exists():
            path.mkdir(parents=True)
            run('git', '-C', path, 'init')
            run('git', '-C', path, 'remote', 'add', 'origin', url)
            run('git', '-C', path, 'fetch', '--depth=1', 'origin', revisions[name])
            run('git', '-C', path, 'checkout', '--detach', 'FETCH_HEAD')
        if run('git', '-C', path, 'rev-parse', 'HEAD') != revisions[name]:
            raise ValueError('Build source revision differs: ' + name)
    print(json.dumps(apply(paths['kernel'], PROJECT / 'firmware/kernel/kaiforward_incall_playback.h')))
    output = root / 'kernel-out-gcc48'
    output.mkdir(exist_ok=True)
    if not (output / '.config').exists():
        config = stock_config.read_text().replace('CONFIG_LOCALVERSION=""', 'CONFIG_LOCALVERSION="-kaiforward"')
        config = config.replace('CONFIG_LOCALVERSION_AUTO=y', '# CONFIG_LOCALVERSION_AUTO is not set')
        (output / '.config').write_text(config)
    host_makefile = paths['gerda'] / 'tools/make_ext4fs/extras/ext4_utils/Makefile'
    text = host_makefile.read_text()
    # GCC 16 defaults to C23; these older host tools require GNU C11 and common symbols.
    text = text.replace('\tgcc ', '\t$(CC) -std=gnu11 -fcommon ')
    host_makefile.write_text(text)
    run('make', '-C', paths['gerda'] / 'tools/make_ext4fs', 'CC=gcc')
    run('make', '-C', paths['gerda'] / 'tools/abootimg', 'CC=gcc -std=gnu11 -fcommon')
    print('Pinned sources, kernel correction, and native image tools prepared')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=PROJECT / 'data/gateway-build')
    parser.add_argument('--stock-config', type=Path, required=True)
    args = parser.parse_args()
    prepare(args.root.resolve(), args.stock_config.resolve())
