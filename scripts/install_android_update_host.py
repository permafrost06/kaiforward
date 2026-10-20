#!/usr/bin/env python3
"""One-time root setup for authenticated static KaiForward updates on the existing Nginx site."""
import argparse
from datetime import datetime, timezone
import grp
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess

HOST_ROOT = Path('/srv/kaiforward-updates')
NGINX_SNIPPET = Path('/etc/nginx/snippets/kaiforward-updates.conf')


def install(bundle, site):
    if os.geteuid() != 0 or not os.environ.get('SUDO_UID'):
        raise SystemExit('Run with sudo from the deployment account')
    owner = int(os.environ['SUDO_UID'])
    group = grp.getgrnam('www-data').gr_gid
    root = HOST_ROOT
    snippet = NGINX_SNIPPET
    include = f'    include {snippet};'
    original = site.read_text()
    marker = '    root /var/www/permafrost06;'
    if include not in original and original.count(marker) != 1:
        raise SystemExit('Expected exactly one permafrost06 web root; inspect the site configuration first')
    manifest = json.loads((bundle / 'public/latest.json').read_text())
    name = manifest['apkUrl']
    if Path(name).name != name or not name.endswith('.apk'):
        raise SystemExit('Invalid APK filename')
    with (bundle / 'public' / name).open('rb') as source:
        if hashlib.sha256(source.read()).hexdigest() != manifest['sha256']:
            raise SystemExit('Staged APK checksum mismatch')
    if (root / '.initialized').exists():
        raise SystemExit('Host is already initialized; use --deploy-host for subsequent releases')
    for directory in (root, root / 'public'):
        directory.mkdir(exist_ok=True, mode=0o750)
        os.chown(directory, owner, group)
    shutil.copyfile(bundle / 'auth.htpasswd', root / 'auth.htpasswd')
    os.chown(root / 'auth.htpasswd', 0, group)
    (root / 'auth.htpasswd').chmod(0o640)
    for filename in (name, 'latest.json'):
        dest = root / 'public' / filename
        shutil.copyfile(bundle / 'public' / filename, dest)
        os.chown(dest, owner, group)
        dest.chmod(0o640)
    backup = site.with_name(site.name + '.before-kaiforward-' + datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S'))
    shutil.copy2(site, backup)
    old_snippet = snippet.read_bytes() if snippet.exists() else None
    try:
        shutil.copyfile(bundle / 'nginx-location.conf', snippet)
        snippet.chmod(0o644)
        if include not in original:
            site.write_text(original.replace(marker, marker + '\n' + include, 1))
        subprocess.run(['nginx', '-t'], check=True)
        subprocess.run(['systemctl', 'reload', 'nginx'], check=True)
    except BaseException:
        site.write_text(original)
        if old_snippet is None:
            snippet.unlink(missing_ok=True)
        else:
            snippet.write_bytes(old_snippet)
        raise
    (root / '.initialized').touch(mode=0o644)
    print('Authenticated updates are live. Site configuration backup:', backup)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--site', type=Path, default=Path('/etc/nginx/sites-available/permafrost06.com'))
    args = parser.parse_args()
    install(Path(__file__).resolve().parent, args.site.resolve())
