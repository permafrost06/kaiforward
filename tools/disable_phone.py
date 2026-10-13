#!/usr/bin/env python3
"""Disable KaiForward, preserve its files, and remove only its permissions."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

from root_shell import run

ROOT = Path(__file__).resolve().parents[1]
APP_ID = 'kaiforward.local'
STAGING = '/data/local/tmp/kaiforward-disable'


def root(command):
    code, output = run(command, timeout=90)
    if code:
        raise RuntimeError(output)
    return output


def adb(*args):
    subprocess.run(['adb', '-s', '22aaa475', *args], check=True, capture_output=True)


def main():
    os.umask(0o077)
    destination = next((ROOT / 'backups').glob('*/manifest.json')).parent / ('before-disable-' + time.strftime('%H%M%S'))
    destination.mkdir()
    root('mkdir -p ' + STAGING + ' && chown shell:shell ' + STAGING + ' && chmod 700 ' + STAGING)
    changed = False
    success = False
    stop_attempted = False
    try:
        stop_attempted = True
        root('stop b2g; i=0; while [ "$(getprop init.svc.b2g)" != stopped ]; do i=$((i+1)); [ "$i" -lt 30 ] || exit 1; sleep 1; done')
        root('cp /data/local/webapps/webapps.json ' + STAGING + '/registry-original.json && cp /data/local/permissions.sqlite ' + STAGING + '/permissions-original.sqlite && chmod 644 ' + STAGING + '/registry-original.json ' + STAGING + '/permissions-original.sqlite')
        for filename in ('registry-original.json', 'permissions-original.sqlite'):
            adb('pull', STAGING + '/' + filename, str(destination / filename))
        registry = json.loads((destination / 'registry-original.json').read_text())
        app = registry.pop(APP_ID, None)
        if not app:
            print('KaiForward is already absent from the registry.')
            success = True
            return
        registry_path = destination / 'registry-disabled.json'
        registry_path.write_text(json.dumps(registry, separators=(',', ':')))
        permissions_path = destination / 'permissions-disabled.sqlite'
        with sqlite3.connect(destination / 'permissions-original.sqlite') as original:
            with sqlite3.connect(permissions_path) as updated:
                original.backup(updated)
                origin = app['origin'] + '^appId=' + str(app['localId'])
                updated.execute('DELETE FROM moz_perms WHERE origin=?', (origin,))
                if updated.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Permission database failed integrity check')
        adb('push', str(registry_path), STAGING + '/registry-disabled.json')
        adb('push', str(permissions_path), STAGING + '/permissions-disabled.sqlite')
        changed = True
        root('cp ' + STAGING + '/registry-disabled.json /data/local/webapps/webapps.json && cp ' + STAGING + '/permissions-disabled.sqlite /data/local/permissions.sqlite && sync')
        success = True
        print('KaiForward registration and its six permissions removed. App files retained.', flush=True)
    finally:
        if changed and not success:
            root('cp ' + STAGING + '/registry-original.json /data/local/webapps/webapps.json && cp ' + STAGING + '/permissions-original.sqlite /data/local/permissions.sqlite && sync')
        if stop_attempted:
            try:
                root('rm -rf ' + STAGING + '; sync')
            finally:
                adb('reboot')
                print('Full phone reboot requested.', flush=True)
    print('Pre-disable state saved in ' + str(destination), flush=True)


if __name__ == '__main__':
    main()
