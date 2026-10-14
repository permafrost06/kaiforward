#!/usr/bin/env python3
"""Prepare call relay server credentials and point the phone at the server.

Copies the phone's token, creates a certificate for the server address if
needed, installs that certificate on the phone as relay-ca.pem and sets the
phone's relay_url. Requires root ADB. Never prints the token.
"""
import argparse
import contextlib
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import tempfile

from scripts.adb import Adb
from scripts.configure_native import write_private
from scripts import settings_native

PROJECT = Path(__file__).resolve().parents[1]
PHONE_HOME = '/data/kaiforward'


def tailscale_ip():
    try:
        output = subprocess.run(['tailscale', 'ip', '-4'], check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise ValueError('Could not read this machine\'s Tailscale IP; pass --host') from None
    return output.split()[0]


def subject_name(host):
    try:
        return 'IP:' + str(ipaddress.ip_address(host))
    except ValueError:
        return 'DNS:' + host


def certificate_covers(certificate, host):
    result = subprocess.run(['openssl', 'x509', '-in', str(certificate), '-noout', '-checkend', '2592000',
                             '-ext', 'subjectAltName'], capture_output=True, text=True)
    if result.returncode:
        return False  # Unreadable, or expires within 30 days.
    wanted = subject_name(host)
    for line in result.stdout.splitlines():
        for entry in line.split(','):
            kind, _, value = entry.strip().partition(':')
            if kind == 'IP Address' and wanted.startswith('IP:'):
                with contextlib.suppress(ValueError):
                    if ipaddress.ip_address(value) == ipaddress.ip_address(host):
                        return True
            elif kind == 'DNS' and wanted == 'DNS:' + value:
                return True
    return False


def create_certificate(directory, host):
    with tempfile.TemporaryDirectory(dir=directory) as work:
        key, certificate = Path(work) / 'relay.key', Path(work) / 'relay.crt'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256',
                        '-nodes', '-days', '3650', '-subj', '/CN=kaiforward-relay',
                        '-addext', 'subjectAltName=' + subject_name(host),
                        '-keyout', str(key), '-out', str(certificate)], check=True, capture_output=True)
        key.chmod(0o600)
        certificate.chmod(0o600)
        os.replace(key, directory / 'relay.key')
        os.replace(certificate, directory / 'relay.crt')


def phone_token(adb):
    # adb pull copies the file exactly; adb shell output on this phone can add stray bytes.
    with tempfile.TemporaryDirectory(prefix='kaiforward-relay-') as work:
        config = Path(work) / 'config'
        adb.run('pull', PHONE_HOME + '/config', str(config))
        lines = config.read_text().splitlines()
    token = next((line[len('token='):] for line in lines if line.startswith('token=')), '')
    if not token:
        raise ValueError('The phone has no token; set one with scripts.settings_native first')
    return token


def setup(serial=None, host=None, port=8769, output=None, renew=False):
    output = output or PROJECT / 'data/call-relay-server'
    host = host or tailscale_ip()
    if not 1 <= port <= 65535:
        raise ValueError('Invalid port')
    relay_url = 'tls://' + ('[' + host + ']' if ':' in host else host) + ':' + str(port)
    adb = Adb(serial)
    if adb.shell('id -u').strip() != '0':
        raise ValueError('Call relay setup needs Gerda root ADB')
    old_umask = os.umask(0o077)
    try:
        output.mkdir(parents=True, exist_ok=True)
        output.chmod(0o700)
        write_private(output / 'token', phone_token(adb) + '\n')
        created = renew or not certificate_covers(output / 'relay.crt', host)
        if created:
            create_certificate(output, host)
    finally:
        os.umask(old_umask)
    target = PHONE_HOME + '/relay-ca.pem'
    adb.run('push', str(output / 'relay.crt'), target + '.new')
    adb.shell('chmod 600 {0}.new && mv {0}.new {0}'.format(target))
    with tempfile.TemporaryDirectory(prefix='kaiforward-relay-') as work:
        patch = Path(work) / 'relay.json'
        patch.write_text(json.dumps({'relay_url': relay_url}))
        settings_native.execute('set', adb.serial, patch)
    return relay_url, created, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='Select the connected phone')
    parser.add_argument('--host', help='Server address the phone connects to (default: this machine\'s Tailscale IPv4)')
    parser.add_argument('--port', type=int, default=8769)
    parser.add_argument('--output', type=Path, help='Server credential directory (default: data/call-relay-server)')
    parser.add_argument('--renew', action='store_true', help='Replace the certificate even if it is still valid')
    args = parser.parse_args()
    relay_url, created, output = setup(args.serial, args.host, args.port, args.output, args.renew)
    host = relay_url[len('tls://'):].rsplit(':', 1)[0].strip('[]')
    print(('Created' if created else 'Kept') + ' certificate ' + str(output / 'relay.crt'))
    print('Copied the phone token to ' + str(output / 'token'))
    print('Phone relay_url: ' + relay_url)
    print('Start the server with:')
    print(f'  python3 call-relay/server.py --token-file {output}/token --certificate {output}/relay.crt '
          f'--key {output}/relay.key --phone-host {host} --phone-port {args.port}')


if __name__ == '__main__':
    main()
