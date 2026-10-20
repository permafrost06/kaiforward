#!/usr/bin/env python3
"""Read or patch the phone's native settings over root ADB without prompts."""
import argparse
import json
from pathlib import Path
import shlex
import tempfile
import uuid

from scripts.adb import Adb

RUNTIME = '/system/bin/kaiforward-native'
FIELDS = {'ssid', 'password', 'url', 'token', 'relay_url', 'app_token'}


def unique_fields(pairs):
    values = {}
    for name, value in pairs:
        if name in values:
            raise ValueError('Duplicate settings field: ' + name)
        values[name] = value
    return values


def execute(action, serial=None, source=None):
    if action not in ('get', 'key', 'set'):
        raise ValueError('Unknown settings action')
    payload = None
    if action == 'set':
        values = json.loads(source.read_text(), object_pairs_hook=unique_fields)
        if not isinstance(values, dict) or not values or set(values) - FIELDS or not all(isinstance(v, str) for v in values.values()):
            raise ValueError('Provide a JSON object with settings string values: ' + ', '.join(sorted(FIELDS)))
        payload = json.dumps(values, ensure_ascii=True).encode()
        if len(payload) > 16384:
            raise ValueError('Settings JSON exceeds 16384 bytes')
    adb = Adb(serial)
    if adb.shell('id -u').strip() != '0':
        raise ValueError('Native settings need Gerda root ADB')
    if action in ('get', 'key'):
        return adb.shell(RUNTIME + (' --settings' if action == 'get' else ' --settings-key'))
    # Stage a private file rather than putting credentials into a shell command.
    target = '/data/kaiforward/settings-input-' + uuid.uuid4().hex + '.json'
    adb.shell('mkdir -p /data/kaiforward && chmod 700 /data/kaiforward')
    with tempfile.TemporaryDirectory(prefix='kaiforward-settings-') as directory:
        path = Path(directory) / 'settings.json'
        path.write_bytes(payload)
        path.chmod(0o600)
        try:
            adb.run('push', str(path), target)
            return adb.shell('chmod 600 ' + shlex.quote(target) + ' && ' + RUNTIME +
                             ' --settings-set < ' + shlex.quote(target))
        finally:
            adb.shell('rm -f ' + shlex.quote(target))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='Select a phone when more than one is connected')
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('get', help='Read settings, with credentials redacted')
    update = commands.add_parser('set', help='Apply a partial JSON settings file')
    update.add_argument('file', type=Path)
    commands.add_parser('key', help='Print the private HTTP settings access key')
    args = parser.parse_args()
    print(execute(args.action, args.serial, getattr(args, 'file', None)), end='')


if __name__ == '__main__':
    main()
