#!/usr/bin/env python3
"""Prepare a signed Android update; stage first-time hosting or publish to an initialized host."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
from urllib.parse import urlparse
import uuid

PROJECT = Path(__file__).resolve().parents[1]
HOST_ROOT = '/srv/kaiforward-updates'
DEFAULT_URL = 'https://kaiforward.permafrost06.com/updates/'


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, **kwargs).stdout


def properties(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if '=' in line and not line.lstrip().startswith('#'))


def manifest_from_badging(badging, apk, base_url, notes):
    package = re.search(r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'", badging, re.M)
    sdk = re.search(r"^sdkVersion:'(\d+)'", badging, re.M)
    if not package or not sdk or package[1] != 'dev.kaiforward.app' or int(sdk[1]) < 26:
        raise ValueError('Not a supported KaiForward APK')
    parsed = urlparse(base_url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('permafrost06.com', 'kaiforward.permafrost06.com')
            or parsed.netloc != parsed.hostname or parsed.query or parsed.fragment
            or not re.fullmatch(r'/[a-zA-Z0-9/_-]+/', parsed.path)):
        raise ValueError('Use an HTTPS update directory on a trusted KaiForward host, ending in /')
    size = apk.stat().st_size
    if not 0 < size <= 100 * 1024 * 1024:
        raise ValueError('APK must be at most 100 MiB')
    with apk.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    filename = f'kaiforward-{package[2]}-{digest[:16]}.apk'
    return {
        'schemaVersion': 1, 'packageName': package[1], 'versionCode': int(package[2]),
        'versionName': package[3], 'apkUrl': filename, 'sha256': digest, 'size': size,
        'minSdk': int(sdk[1]), 'notes': notes,
    }


def prepare(apk, output, sdk, base_url=DEFAULT_URL, notes=''):
    tools = sorted((sdk / 'build-tools').glob('*/aapt'), key=lambda p: tuple(int(n) for n in p.parent.name.split('.')))
    if not tools:
        raise ValueError('Android SDK build-tools are required')
    aapt = tools[-1]
    signer_output = run(str(aapt.with_name('apksigner')), 'verify', '--print-certs', str(apk), text=True)
    # Require the existing release key, preventing accidental publication of debug or unrelated builds.
    signing = properties(PROJECT / 'data/android-release/signing.properties')
    env = dict(os.environ, KAIFORWARD_KEY_PASSWORD=signing['storePassword'])
    cert = run('keytool', '-exportcert', '-keystore', str((PROJECT / 'android' / signing['storeFile']).resolve()),
               '-storepass:env', 'KAIFORWARD_KEY_PASSWORD', '-alias', signing['keyAlias'], env=env)
    fingerprint = hashlib.sha256(cert).hexdigest()
    fingerprints = re.findall(r'Signer #\d+ certificate SHA-256 digest: ([0-9a-f]+)', signer_output)
    if fingerprints != [fingerprint]:
        raise ValueError('APK must be signed with the configured KaiForward release key')
    manifest = manifest_from_badging(run(str(aapt), 'dump', 'badging', str(apk), text=True), apk, base_url, notes)
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    public = output / 'public'
    public.mkdir(exist_ok=True)
    shutil.copyfile(apk, public / manifest['apkUrl'])
    (public / 'latest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    token = properties(PROJECT / 'data/android-viewer.env')['KAIFORWARD_READ_TOKEN']
    password_hash = run('openssl', 'passwd', '-6', '-stdin', input=token + '\n', text=True).strip()
    (output / 'auth.htpasswd').write_text('kaiforward:' + password_hash + '\n')
    path = urlparse(base_url).path
    (output / 'nginx-location.conf').write_text(f'''# Include inside the HTTPS server block for {urlparse(base_url).hostname}.
location ^~ {path} {{
    alias {HOST_ROOT}/public/;
    auth_basic "KaiForward updates";
    auth_basic_user_file {HOST_ROOT}/auth.htpasswd;
    autoindex off;
    add_header Cache-Control "private, no-store" always;
    add_header X-Content-Type-Options "nosniff" always;
    types {{ application/json json; application/vnd.android.package-archive apk; }}
    default_type application/octet-stream;
    limit_except GET {{ deny all; }}
}}
''')
    shutil.copyfile(PROJECT / 'scripts/install_android_update_host.py', output / 'install-host.py')
    shutil.copyfile(PROJECT / 'scripts/migrate_android_update_host.py', output / 'migrate-subdomain.py')
    print(f'Prepared KaiForward {manifest["versionName"]} ({manifest["versionCode"]}): {output}')
    return manifest


def upload(output, host, initialize=False):
    if not re.fullmatch(r'[a-zA-Z0-9_.@-]+', host) or host.startswith('-'):
        raise ValueError('Invalid SSH host')
    if initialize:
        # Outside the web root; no APK becomes public before authentication is configured.
        run('ssh', host, 'mkdir -p -m 700 ~/kaiforward-update-staging')
        run('scp', '-r', str(output) + '/.', host + ':kaiforward-update-staging/')
        print(f'Staged privately on {host}. One-time setup:')
        print(f'ssh -t {host} sudo python3 kaiforward-update-staging/install-host.py')
        return
    manifest = json.loads((output / 'public/latest.json').read_text())
    release = manifest['apkUrl']
    transfer_id = uuid.uuid4().hex
    pending = '.latest-' + transfer_id + '.json'
    pending_apk = '.apk-' + transfer_id + '.part'
    # Fail closed unless the first-time installer has completed.
    run('ssh', host, f'test -f {HOST_ROOT}/.initialized && test -w {HOST_ROOT}/public')
    # Publish immutable APK first and replace the manifest only after the remote hash matches.
    run('scp', str(output / 'public' / release), f'{host}:{HOST_ROOT}/public/{pending_apk}')
    run('scp', str(output / 'public/latest.json'), f'{host}:{HOST_ROOT}/public/{pending}')
    run('ssh', host, f'python3 - {shlex.quote(HOST_ROOT)} {shlex.quote(release)} {shlex.quote(pending)} {shlex.quote(pending_apk)} '
        f'{manifest["sha256"]} {manifest["versionCode"]}', input='''import hashlib,json,pathlib,sys
import fcntl
root, name, pending, pending_apk, expected, version = sys.argv[1:]
public = pathlib.Path(root) / 'public'
apk = public / pending_apk
with apk.open('rb') as source:
    assert hashlib.sha256(source.read()).hexdigest() == expected, 'Remote APK checksum mismatch'
with (pathlib.Path(root) / '.publish.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    current = json.loads((public / 'latest.json').read_text())
    assert int(version) > current['versionCode'] or (int(version) == current['versionCode'] and
        expected == current['sha256']), 'Increment versionCode before publishing a different APK'
    apk.chmod(0o644)
    apk.replace(public / name)
    (public / pending).chmod(0o644)
    (public / pending).replace(public / 'latest.json')
''', text=True)
    print(f'Published version {manifest["versionCode"]} on {host}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, default=PROJECT / 'android/app/build/outputs/apk/release/app-release.apk')
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist/android-update')
    parser.add_argument('--sdk', type=Path, default=Path(os.environ.get('ANDROID_HOME', str(Path.home() / 'Android/Sdk'))))
    parser.add_argument('--base-url', default=DEFAULT_URL)
    parser.add_argument('--notes', default='')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--stage-host', help='Upload privately for first-time Nginx setup')
    action.add_argument('--deploy-host', help='Publish to a host already initialized by install-host.py')
    args = parser.parse_args()
    if args.stage_host and args.base_url != 'https://permafrost06.com/kaiforward/updates/':
        parser.error('First-time setup requires --base-url https://permafrost06.com/kaiforward/updates/; then run migrate-subdomain.py')
    os.umask(0o077)
    prepare(args.apk.resolve(), args.output.resolve(), args.sdk.resolve(), args.base_url, args.notes)
    if args.stage_host or args.deploy_host:
        upload(args.output.resolve(), args.stage_host or args.deploy_host, initialize=bool(args.stage_host))


if __name__ == '__main__':
    main()
