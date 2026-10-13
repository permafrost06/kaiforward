#!/usr/bin/env python3
"""Register only KaiForward while KaiOS is stopped; restore state on failure."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import zipfile

from root_shell import run

ROOT = Path(__file__).resolve().parents[1]
APP_ID = 'kaiforward.local'
STAGING = '/data/local/tmp/kaiforward-install'
PROFILE = '/data/b2g/mozilla/tzzh0tct.default'


def root(command, timeout=90):
    code, output = run(command, timeout=timeout)
    if code:
        raise RuntimeError(output)
    return output


def adb(*args):
    subprocess.run(['adb', '-s', '22aaa475', *args], check=True, capture_output=True)


def main():
    os.umask(0o077)
    backup = next((ROOT / 'backups').glob('*/manifest.json')).parent / ('before-app-install-' + time.strftime('%H%M%S'))
    backup.mkdir(exist_ok=False)
    root('mkdir -p ' + STAGING + ' && chown shell:shell ' + STAGING + ' && chmod 700 ' + STAGING)
    package = ROOT / 'dist' / 'kaiforward.zip'
    adb('push', str(package), STAGING + '/application.zip')
    with zipfile.ZipFile(package) as archive:
        manifest_bytes = archive.read('manifest.webapp')
    manifest = json.loads(manifest_bytes)
    (backup / 'manifest.webapp').write_bytes(manifest_bytes)
    adb('push', str(backup / 'manifest.webapp'), STAGING + '/manifest.webapp')
    stop_attempted = False
    changed = False
    successful = False
    try:
        stop_attempted = True
        root('stop b2g; i=0; while [ "$(getprop init.svc.b2g)" != stopped ]; do i=$((i+1)); [ "$i" -lt 30 ] || exit 1; sleep 1; done')
        print('KaiOS stopped; saving app registry and permissions.', flush=True)
        root('cp /data/local/webapps/webapps.json ' + STAGING + '/webapps-original.json && cp /data/local/permissions.sqlite ' + STAGING + '/permissions-original.sqlite && chmod 644 ' + STAGING + '/webapps-original.json ' + STAGING + '/permissions-original.sqlite')
        for name in ('webapps-original.json', 'permissions-original.sqlite'):
            adb('pull', STAGING + '/' + name, str(backup / name))
        registry = json.loads((backup / 'webapps-original.json').read_text())
        if APP_ID in registry:
            raise RuntimeError('KaiForward already exists; use its normal update path.')
        local_id = max(app['localId'] for app in registry.values()) + 1
        stamp = int(time.time() * 1000)
        registry[APP_ID] = {
            'origin': 'app://' + APP_ID, 'installOrigin': 'app://' + APP_ID,
            'manifestURL': 'app://' + APP_ID + '/manifest.webapp',
            'localId': local_id, 'appStatus': 3, 'basePath': '/data/local/webapps',
            'id': APP_ID, 'name': manifest['name'], 'removable': True, 'sideloaded': True,
            'kind': 'packaged', 'enabled': True, 'installState': 'installed',
            'installTime': stamp, 'updateTime': stamp, 'receipts': [],
            'manifestHash': hashlib.md5(json.dumps(manifest, separators=(',', ':')).encode()).hexdigest(),
            'csp': manifest['csp'], 'role': '', 'widgetPages': [], 'redirects': None,
            'additionalLanguages': {}, 'installerAppId': 0, 'installerIsBrowser': False,
            'storeId': '', 'storeVersion': 0, 'blockedStatus': 0,
            'downloading': False, 'readyToApplyDownload': False
        }
        updated_registry = backup / 'webapps-updated.json'
        updated_registry.write_text(json.dumps(registry, separators=(',', ':')))
        updated_permissions = backup / 'permissions-updated.sqlite'
        with sqlite3.connect(backup / 'permissions-original.sqlite') as original:
            with sqlite3.connect(updated_permissions) as database:
                original.backup(database)
                origin = 'app://' + APP_ID + '^appId=' + str(local_id)
                for permission in ('sms', 'alarms', 'systemXHR', 'power', 'indexedDB', 'indexedDB-unlimited'):
                    database.execute('INSERT INTO moz_perms (origin,type,permission,expireType,expireTime,modificationTime) VALUES (?,?,1,0,0,?)', (origin, permission, stamp))
                if database.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise RuntimeError('Permission database failed integrity check')
        adb('push', str(updated_registry), STAGING + '/webapps-updated.json')
        adb('push', str(updated_permissions), STAGING + '/permissions-updated.sqlite')
        changed = True
        root('mkdir /data/local/webapps/' + APP_ID + ' && cp ' + STAGING + '/application.zip ' + STAGING + '/manifest.webapp /data/local/webapps/' + APP_ID + '/ && chmod 700 /data/local/webapps/' + APP_ID + ' && chmod 600 /data/local/webapps/' + APP_ID + '/* && cp ' + STAGING + '/webapps-updated.json /data/local/webapps/webapps.json && cp ' + STAGING + '/permissions-updated.sqlite /data/local/permissions.sqlite && restorecon -R /data/local/webapps/' + APP_ID + ' && sync')
        # Restore original debugger restrictions; no system partition was modified.
        root('rm -f ' + PROFILE + '/user.js')
        successful = True
        print('Registered KaiForward with app ID ' + str(local_id) + '.', flush=True)
    finally:
        if changed and not successful:
            root('cp ' + STAGING + '/webapps-original.json /data/local/webapps/webapps.json && cp ' + STAGING + '/permissions-original.sqlite /data/local/permissions.sqlite && rm -rf /data/local/webapps/' + APP_ID + ' && sync')
            print('Original registry and permissions restored.', flush=True)
        if stop_attempted:
            # An interface-only restart left the radio state unusable on this phone.
            # Reboot all services together instead, including after rollback.
            try:
                root('rm -rf ' + STAGING + '; rm -f /data/local/tmp/kaiforward-profile.tar /data/local/tmp/kaiforward-permissions.sqlite /data/local/tmp/kaiforward-webapps.json /data/local/tmp/kaiforward-boot.img /data/local/tmp/kaiforward-debug-prefs.js; sync')
            finally:
                adb('reboot')
                print('Full phone reboot requested.', flush=True)


if __name__ == '__main__':
    main()
