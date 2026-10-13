#!/usr/bin/env python3
"""Copy firmware partitions without writing any phone block device."""
import datetime
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from root_shell import run

SERIAL = '22aaa475'


def adb(*args):
    return subprocess.run(['adb', '-s', SERIAL, *args], check=True, capture_output=True).stdout


def root(command, timeout=300):
    code, output = run(command, timeout=timeout)
    if code:
        raise RuntimeError(output)
    return output


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--resume', type=Path, help='Resume an interrupted backup after rechecking saved hashes')
    args = parser.parse_args()
    destination = args.resume or Path('backups') / ('TA-1059-12.00.17.10-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    destination.mkdir(parents=True, exist_ok=bool(args.resume))
    listing = root('ls -l /dev/block/bootdevice/by-name')
    names = sorted(re.findall(r'\s(\w+) -> /dev/block/mmcblk0p\d+', listing))
    excluded = {'userdata', 'cache'}
    manifest = {'device': 'TA-1059', 'reported_firmware': '12.00.17.10', 'serial': SERIAL,
                'excluded_partitions': sorted(excluded), 'partitions': {},
                'note': 'Live firmware backup, not a full user-data backup. No block device was written.'}
    if args.resume:
        manifest = json.loads((destination / 'manifest.json').read_text())
        if manifest['serial'] != SERIAL or manifest['excluded_partitions'] != sorted(excluded):
            raise RuntimeError('Backup identity or scope does not match')
        for name, entry in manifest['partitions'].items():
            image = destination / (name + '.img')
            with image.open('rb') as source:
                checksum = hashlib.file_digest(source, 'sha256').hexdigest()
            if image.stat().st_size != entry['bytes'] or checksum != entry['sha256']:
                raise RuntimeError('Saved backup verification failed: ' + name)
    (destination / 'partitions.txt').write_text(root('cat /proc/partitions; ls -l /dev/block/bootdevice/by-name'))
    (destination / 'getprop.txt').write_text(root('getprop'))
    staging = '/data/local/tmp/kaiforward-backup.img'
    for name in names:
        if name in excluded:
            continue
        if name in manifest['partitions']:
            print('Reverified saved ' + name, flush=True)
            continue
        print('Backing up ' + name, flush=True)
        result = root('busybox dd if=/dev/block/bootdevice/by-name/' + name + ' of=' + staging +
                      ' bs=1048576 && chmod 644 ' + staging + ' && busybox sha256sum ' + staging)
        expected = re.search(r'([a-f0-9]{64})\s+' + re.escape(staging), result)[1]
        image = destination / (name + '.img')
        adb('pull', staging, str(image))
        checksum = hashlib.file_digest(image.open('rb'), 'sha256').hexdigest()
        if checksum != expected:
            raise RuntimeError('Checksum mismatch for ' + name + '; staging retained')
        manifest['partitions'][name] = {'bytes': image.stat().st_size, 'sha256': checksum}
        (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        root('rm -f ' + staging)
    print('Verified backup: ' + str(destination), flush=True)


if __name__ == '__main__':
    main()
