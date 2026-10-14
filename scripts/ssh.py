"""Install key-only SSH on native firmware over root ADB. Never reboots."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid

from firmware.scripts.tailscale_boot import patch_boot
from scripts.adb import Adb, AdbError
from scripts.files import file_sha256
from scripts import tailscale

PROJECT = Path(__file__).resolve().parents[1]
RUNTIME = '/data/kaiforward/ssh'
STARTUP = '/system/kaiforward/ssh.sh'
BOOT = tailscale.BOOT
SERVICE = b'''service kf-ssh /system/bin/sh /system/kaiforward/ssh.sh
    class main
    user root
    group root inet
'''
FILES = {
    'dropbear': (RUNTIME + '/dropbear', '700'),
    'dropbearkey': (RUNTIME + '/dropbearkey', '700'),
    'authorized_keys': (RUNTIME + '/.ssh/authorized_keys', '600'),
    'startup.sh': (STARTUP, '755'),
    'DROPBEAR-LICENSE': (RUNTIME + '/DROPBEAR-LICENSE', '600'),
}


READY = r'''if [ ! -f "$runtime/disabled" ]; then
    tries=0
    while :; do
        sleep 1
        if [ -r "$runtime/dropbear.pid" ] && alive_pid "$(cat "$runtime/dropbear.pid")"; then
            break
        fi
        tries=$((tries + 1))
        [ "$tries" -lt 10 ] || { echo 'SSH failed to start; see ssh.log' >&2; exit 1; }
    done
fi
'''


def bundle_scripts(bundle, manifest):
    """Reuse the verified install/rollback transaction, with SSH lifecycle commands."""
    scripts = tailscale.bundle_scripts(bundle, manifest, 'unused')
    version_check = '''busybox chmod 700 "$bundle/tailscale"
[ "$("$bundle/tailscale" version | busybox head -1)" = 'unused' ]'''
    for name, body in scripts.items():
        body = body.replace(version_check, 'busybox chmod 700 "$bundle/dropbear"\n"$bundle/dropbear" -V')
        body = body.replace('stop_pid "$runtime/tailscaled.pid" "$runtime/tailscaled"',
                            'stop_pid "$runtime/dropbear.pid" "$runtime/dropbear"')
        body = body.replace('    stop_pid "$runtime/supervisor.pid" "$startup"\n', '')
        body = body.replace('        echo $! >"$runtime/supervisor.pid"', ':')
        body = body.replace('[ -x "$runtime/tailscaled" ]', '[ -x "$runtime/dropbear" ]')
        body = body.replace('tailscale', 'ssh').replace('Tailscale', 'SSH')
        body = body.replace('busybox mkdir -p "$runtime"', 'busybox mkdir -p "$runtime/.ssh"\nbusybox chmod 700 "$runtime/.ssh"')
        if name == 'apply.sh':
            body = body.replace('start_runtime\ntouch', 'start_runtime\n' + READY + 'touch')
        scripts[name] = body
    return scripts


def phone(serial):
    adb = Adb(serial)
    if adb.shell('id -u').strip() != '0' or adb.shell('uname -m').strip() != 'armv7l':
        raise ValueError('SSH needs root ADB on the native ARMv7 firmware')
    adb.shell('test -x /system/bin/kaiforward-native && test -r /init.kaiforward-native.rc')
    return adb


def validate_runtime(runtime):
    pinned = json.loads((runtime / 'ssh-manifest.json').read_text())
    from firmware.scripts.build_ssh import VERSION, SHA256
    if pinned['version'] != VERSION or pinned['source_sha256'] != SHA256:
        raise ValueError('Unsupported SSH runtime build')
    for name in ('ssh-account.h', 'ssh-options.h'):
        if pinned['configuration'][name] != file_sha256(PROJECT / 'firmware/native' / name):
            raise ValueError('Rebuild SSH after changing ' + name)
    for name in ('dropbear', 'dropbearkey', 'DROPBEAR-LICENSE'):
        if file_sha256(runtime / name) != pinned['files'][name]:
            raise ValueError('SSH runtime checksum mismatch: ' + name)


def prepare(cache=None, runtime=None, zig=None):
    """Reuse a verified build, or build the pinned server on the PC."""
    if runtime is not None:
        validate_runtime(runtime)
        return runtime
    from firmware.scripts.build_ssh import build
    if zig is not None and not zig.is_file():
        raise ValueError('Zig compiler does not exist: ' + str(zig))
    cache = (cache or PROJECT / 'data/ssh-install').resolve()
    runtime = cache / 'runtime'
    try:
        validate_runtime(runtime)
    except (OSError, ValueError, KeyError):
        print('Building the pinned ARM SSH runtime...')
        build(cache / 'build', runtime,
              zig or PROJECT / 'data/native-build/zig-aarch64-linux-0.15.2/zig')
        validate_runtime(runtime)
    return runtime


def install(serial=None, runtime=None, public_key=None, cache=None, zig=None):
    if public_key is None:
        raise ValueError('Provide an Ed25519 public key explicitly with --public-key')
    key = public_key.read_text().strip()
    if not re.fullmatch(r'ssh-ed25519 [A-Za-z0-9+/]+={0,2}(?: [^\r\n]*)?', key):
        raise ValueError('Provide a single OpenSSH Ed25519 public key')
    # ssh-keygen checks the decoded key, not just its textual form.
    subprocess.run(['ssh-keygen', '-l', '-f', str(public_key)], check=True, capture_output=True)
    runtime = prepare(cache, runtime, zig)
    adb = phone(serial)
    install_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    remote = '/data/kaiforward/ssh-installs/' + install_id
    local = PROJECT / 'backups/ssh' / (adb.serial + '-' + install_id)
    local.mkdir(parents=True, mode=0o700)
    adb.shell('umask 077; mkdir -p ' + shlex.quote(remote))
    adb.shell('busybox dd if=' + BOOT + ' of=' + remote + '/boot-before.img bs=1048576')
    digest = adb.shell('busybox sha256sum ' + remote + '/boot-before.img').split()[0]
    adb.run('pull', remote + '/boot-before.img', str(local / 'boot-before.img'))
    if file_sha256(local / 'boot-before.img') != digest:
        raise ValueError('Boot backup hash mismatch')
    patched, manifest = patch_boot((local / 'boot-before.img').read_bytes(), SERVICE)
    (local / 'boot-installed.img').write_bytes(patched)
    for name in ('dropbear', 'dropbearkey', 'DROPBEAR-LICENSE'):
        shutil.copyfile(runtime / name, local / name)
    (local / 'authorized_keys').write_text(key + '\n')
    shutil.copyfile(PROJECT / 'firmware/native/ssh.sh', local / 'startup.sh')
    desired = {name: file_sha256(local / name) for name in FILES}
    current = adb.shell('for target in ' + ' '.join(shlex.quote(target) for target, _ in FILES.values()) +
                        '; do if [ -f "$target" ]; then busybox sha256sum "$target"; fi; done')
    installed = {line.split(maxsplit=1)[1].strip(): line.split()[0] for line in current.splitlines()}
    if not manifest['changed'] and all(installed.get(FILES[name][0]) == digest for name, digest in desired.items()):
        print('SSH files and boot startup are already installed; no payload or boot writes needed.')
        print('Boot backup: ' + str(local / 'boot-before.img'))
        enabled = adb.shell('if [ -f ' + RUNTIME + '/disabled ]; then echo disabled; fi').strip() != 'disabled'
        if enabled:
            manage('start', adb=adb)
        else:
            print('SSH remains disabled; run python3 -m scripts.ssh start to enable it.')
        return dict(manifest, changed=False, backup=str(local))
    (local / 'files').write_text(''.join(f'{name} {target} {mode}\n' for name, (target, mode) in FILES.items()))
    for name, body in bundle_scripts(remote, manifest).items():
        (local / name).write_text(body)
        subprocess.run(['sh', '-n', str(local / name)], check=True)
    (local / 'installation.json').write_text(json.dumps(manifest, indent=2) + '\n')
    names = [*FILES, 'files', 'boot-before.img', 'boot-installed.img', 'apply.sh', 'rollback.sh']
    (local / 'SHA256SUMS').write_text(''.join(file_sha256(local / n) + '  ' + n + '\n' for n in names))
    for name in [n for n in names if n != 'boot-before.img'] + ['SHA256SUMS']:
        adb.run('push', str(local / name), remote + '/' + name)
    print('Boot backup: ' + str(local / 'boot-before.img'))
    print('Rollback: python3 -m scripts.ssh' + (' --serial ' + shlex.quote(serial) if serial else '') + ' rollback ' + install_id)
    print(adb.shell('/system/bin/sh ' + remote + '/apply.sh', timeout=180).strip())
    if manifest['changed']:
        print('Boot startup added. Restart the phone when ready; no reboot performed.')
    print('SSH installed. Host keys and saved enabled/disabled state retained.')
    return dict(manifest, install_id=install_id, backup=str(local))


def manage(action, serial=None, adb=None, installation_id=None):
    adb = adb or phone(serial)
    if action == 'rollback':
        if not installation_id or not tailscale.INSTALL_ID.fullmatch(installation_id):
            raise ValueError('Invalid installation ID')
        print(adb.shell('/system/bin/sh /data/kaiforward/ssh-installs/' + installation_id + '/rollback.sh', timeout=180))
    elif action == 'status':
        service = adb.shell('getprop init.svc.kf-ssh').strip()
        disabled = adb.shell('if [ -f ' + RUNTIME + '/disabled ]; then echo yes; fi').strip()
        print('Boot service: ' + (service or 'not loaded; reboot needed if newly installed'))
        print('Startup enabled: ' + ('no' if disabled else 'yes'))
        print(adb.shell('if [ -r ' + RUNTIME + '/ssh.log ]; then tail -10 ' + RUNTIME + '/ssh.log; fi'))
    elif action in ('start', 'stop'):
        if action == 'start':
            adb.shell('test -x ' + RUNTIME + '/dropbear && test -s ' + RUNTIME + '/.ssh/authorized_keys && test -r ' + STARTUP)
        adb.shell(('touch ' if action == 'stop' else 'rm -f ') + RUNTIME + '/disabled')
        body = bundle_scripts('/unused', {'original_sha256': '', 'patched_sha256': '', 'bytes': 0})['apply.sh'].split('bundle=/unused')[0]
        if action == 'start':
            body += r'''
if [ -r "$runtime/dropbear.pid" ] && alive_pid "$(cat "$runtime/dropbear.pid")"; then
    busybox tr '\000' ' ' <"/proc/$(cat "$runtime/dropbear.pid")/cmdline" | busybox grep -F -q "$runtime/dropbear"
else
    start_runtime
fi
''' + READY
        else:
            body += '\nstop_runtime\n'
        with tempfile.TemporaryDirectory(prefix='kaiforward-ssh-') as folder:
            path = Path(folder) / 'manage.sh'
            path.write_text(body)
            remote = '/data/local/tmp/ssh-manage-' + uuid.uuid4().hex + '.sh'
            try:
                adb.run('push', str(path), remote)
                adb.shell('/system/bin/sh ' + remote)
            finally:
                adb.shell('rm -f ' + remote)
        print('SSH ' + ('started; boot startup enabled.' if action == 'start' else 'stopped; disabled across reboot.'))
    else:
        raise ValueError('Unknown SSH action')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='Select the connected phone')
    commands = parser.add_subparsers(dest='action', required=True)
    cmd = commands.add_parser('install', help='Build and install key-only SSH and boot startup without rebooting')
    cmd.add_argument('--runtime', type=Path, help='Use an already built, verified runtime')
    cmd.add_argument('--cache', type=Path, help='Download/build cache (default: data/ssh-install)')
    cmd.add_argument('--zig', type=Path, help='Use an existing Zig compiler')
    cmd.add_argument('--public-key', type=Path, required=True,
                     help='Ed25519 public key to authorize on the phone')
    for action in ('status', 'stop', 'start'):
        commands.add_parser(action)
    cmd = commands.add_parser('rollback', help='Restore the preceding boot and payload, retaining host keys')
    cmd.add_argument('installation_id')
    args = parser.parse_args()
    try:
        if args.action == 'install':
            install(args.serial, args.runtime, args.public_key, args.cache, args.zig)
        else:
            manage(args.action, args.serial, installation_id=getattr(args, 'installation_id', None))
    except (AdbError, ValueError, OSError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
