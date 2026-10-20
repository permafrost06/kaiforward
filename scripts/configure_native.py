#!/usr/bin/env python3
"""Prepare private native settings, optionally copying them to Gerda over USB."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
from scripts.adb import Adb
import tempfile
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[1]


def encode_config(values):
    fields = ['ssid', 'password', 'url', 'token', 'relay_url', 'app_token']
    values = {'relay_url': '', 'app_token': '', **values}
    if set(values) != set(fields) or not all(isinstance(v, str) for v in values.values()):
        raise ValueError('Invalid settings fields')
    if any(any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values.values()):
        raise ValueError('Settings cannot contain control characters')
    if not 1 <= len(values['ssid'].encode()) <= 32:
        raise ValueError('Wi-Fi name must contain 1 to 32 UTF-8 bytes')
    password = values['password']
    if password and not (8 <= len(password.encode()) <= 63 or re.fullmatch(r'[0-9a-fA-F]{64}', password)):
        raise ValueError('Wi-Fi password must be 8–63 bytes or a 64-digit hexadecimal PSK')
    parsed = urlsplit(values['url'])
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Provide an HTTP(S) receiver URL without embedded credentials or fragment')
    if any(c in values['url'] + values['token'] for c in ' "\\'):
        raise ValueError('URL/token contains an unsupported character')
    for name, limit in [('url', 1024), ('token', 256)]:
        if not 1 <= len(values[name].encode()) <= limit:
            raise ValueError(name + ' is empty or too long')
    if len(values['app_token']) > 256 or any(ord(c) <= 32 or ord(c) >= 127 for c in values['app_token']):
        raise ValueError('App token must contain at most 256 printable ASCII characters without spaces')
    relay_url = values['relay_url']
    if relay_url:
        relay = urlsplit(relay_url)
        try:
            port = relay.port
        except ValueError:
            port = 0
        if relay.scheme != 'tls' or not relay.hostname or relay.username or port == 0 \
                or relay.path or '?' in relay_url or '#' in relay_url \
                or any(c in relay_url for c in ' "\\') or len(relay_url.encode()) > 256:
            raise ValueError('Provide the call relay as tls://host[:port]')
    return ''.join(name + '=' + values[name] + '\n' for name in fields)


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='native-settings-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def install(directory, serial):
    adb = Adb(serial)
    # Gerda exposes root ADB. Do not modify the stock KaiOS app/profile or boot.
    uid = adb.shell('id -u').strip()
    if uid != '0':
        raise ValueError('Native provisioning needs Gerda root ADB; current phone is not root')
    adb.shell('test -x /system/bin/kaiforward-native')
    adb.shell('mkdir -p /data/kaiforward && chmod 700 /data/kaiforward')
    for name in ['config', 'ca.pem', 'relay-ca.pem']:
        path = directory / name
        if path.exists():
            target = '/data/kaiforward/' + name
            adb.run('push', str(path), target + '.new')
            adb.shell('chmod 600 ' + target + '.new && mv ' + target + '.new ' + target)
    adb.shell('touch /data/kaiforward/reload /data/kaiforward/sync')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssid')
    parser.add_argument('--receiver-url', help='Complete URL, including /v1/messages')
    parser.add_argument('--receiver-config', type=Path, default=PROJECT / 'data/config.json', help='PC config containing the receiver token')
    parser.add_argument('--ca', type=Path, help='Trusted CA bundle, or certificate for a private HTTPS receiver')
    parser.add_argument('--relay-url', help='Call relay server as tls://host[:port]; empty disables calls')
    parser.add_argument('--relay-ca', type=Path, help='Certificate trusted for the call relay, when not publicly signed')
    parser.add_argument('--output', type=Path, default=PROJECT / 'data/native-settings')
    parser.add_argument('--install', action='store_true', help='Copy settings to an installed native Gerda phone')
    parser.add_argument('--serial')
    args = parser.parse_args()
    os.umask(0o077)
    existing = {}
    if (args.output / 'config').exists():
        existing = dict(line.split('=', 1) for line in (args.output / 'config').read_text().splitlines() if line and not line.startswith('#'))
    receiver = json.loads(args.receiver_config.read_text()) if args.receiver_config.exists() else {}
    ssid = args.ssid or input('Wi-Fi network [' + existing.get('ssid', '') + ']: ') or existing.get('ssid', '')
    password = getpass.getpass('Wi-Fi password (blank keeps saved value; type OPEN for an open network): ')
    password = '' if password == 'OPEN' else password or existing.get('password', '')
    url = args.receiver_url or input('Receiver URL [' + existing.get('url', '') + ']: ') or existing.get('url', '')
    token = receiver.get('token') or existing.get('token') or getpass.getpass('Receiver token: ')
    relay_url = existing.get('relay_url', '') if args.relay_url is None else args.relay_url
    values = dict(ssid=ssid, password=password, url=url, token=token, relay_url=relay_url, app_token=existing.get('app_token', ''))
    write_private(args.output / 'config', encode_config(values))
    ca = args.ca
    if url.startswith('https://') and not ca and not (args.output / 'ca.pem').exists():
        ca = next((p for p in [Path('/etc/pki/tls/certs/ca-bundle.crt'), Path('/etc/ssl/certs/ca-certificates.crt')] if p.exists()), None)
        if ca is None:
            raise ValueError('Provide --ca for this HTTPS destination')
    if ca:
        write_private(args.output / 'ca.pem', ca.read_text())
    if args.relay_ca:
        write_private(args.output / 'relay-ca.pem', args.relay_ca.read_text())
    if args.install:
        install(args.output, args.serial)
    print('Native settings ' + ('installed on phone' if args.install else 'saved to ' + str(args.output)) + '; credentials were not printed.')


if __name__ == '__main__':
    main()
