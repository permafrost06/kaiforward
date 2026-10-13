#!/usr/bin/env python3
"""Install and manage Tailscale on native Nokia 8110 firmware over root ADB. Never reboots."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid

from firmware.scripts.tailscale_boot import patch_boot
from scripts.adb import Adb, AdbError
from scripts.files import file_sha256

PROJECT = Path(__file__).resolve().parents[1]
NATIVE = PROJECT / 'firmware/native'
RUNTIME = '/data/kaiforward/tailscale'
STARTUP = '/system/kaiforward/tailscale.sh'
BOOT = '/dev/block/bootdevice/by-name/boot'
INSTALLS = '/data/kaiforward/tailscale-installs'
FILES = {
    'tailscale': (RUNTIME + '/tailscale', '700'),
    'tailscaled': (RUNTIME + '/tailscaled', '700'),
    'ca-certificates.crt': (RUNTIME + '/ca-certificates.crt', '600'),
    'startup.sh': (STARTUP, '755'),
    'installation.json': (RUNTIME + '/installation.json', '600'),
}
INSTALL_ID = re.compile(r'\d{8}-\d{6}-[a-f0-9]{8}\Z')


def inputs():
    return json.loads((NATIVE / 'tailscale-inputs.json').read_text())


def prepare(cache, archive=None, ca_bundle=None):
    """Verify the pinned archive before extracting only its two ARM executables."""
    pinned = inputs()
    cache.mkdir(parents=True, exist_ok=True)
    if archive is None:
        archive = cache / Path(pinned['url']).name
        if not archive.exists():
            partial = archive.with_suffix('.partial-' + uuid.uuid4().hex)
            try:
                with urllib.request.urlopen(pinned['url'], timeout=30) as source, partial.open('wb') as dest:
                    shutil.copyfileobj(source, dest)
                if file_sha256(partial) != pinned['sha256']:
                    raise ValueError('Downloaded Tailscale archive failed its pinned SHA-256 check')
                partial.replace(archive)
            finally:
                partial.unlink(missing_ok=True)
    if file_sha256(archive) != pinned['sha256']:
        raise ValueError('Tailscale archive does not match firmware/native/tailscale-inputs.json')
    output = cache / ('runtime-' + pinned['version'])
    output.mkdir(exist_ok=True)
    with tarfile.open(archive, 'r:gz') as bundle:
        for name in ('tailscale', 'tailscaled'):
            member = bundle.getmember(f'tailscale_{pinned["version"]}_arm/{name}')
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise ValueError('Invalid executable archive member: ' + name)
            with bundle.extractfile(member) as source, (output / name).open('wb') as dest:
                shutil.copyfileobj(source, dest)
            header = (output / name).read_bytes()[:20]
            if header[:7] != b'\x7fELF\x01\x01\x01' or int.from_bytes(header[18:20], 'little') != 40:
                raise ValueError('Tailscale executable is not 32-bit little-endian ARM: ' + name)
            (output / name).chmod(0o700)
    if ca_bundle is None:
        ca_bundle = next((p for p in [Path('/etc/ssl/certs/ca-certificates.crt'),
                                     Path('/etc/pki/tls/certs/ca-bundle.crt')] if p.is_file()), None)
    if ca_bundle is None or b'-----BEGIN CERTIFICATE-----' not in ca_bundle.read_bytes():
        raise ValueError('Provide a public CA bundle using --ca-bundle')
    shutil.copyfile(ca_bundle, output / 'ca-certificates.crt')
    shutil.copyfile(NATIVE / 'tailscale.sh', output / 'startup.sh')
    return output, pinned


def phone(serial=None):
    adb = Adb(serial)
    if adb.shell('id -u').strip() != '0':
        raise ValueError('Tailscale installation needs root ADB on the custom firmware')
    if adb.shell('uname -m').strip() != 'armv7l':
        raise ValueError('This installer supports the Nokia 8110 ARMv7 firmware')
    adb.shell('test -x /system/bin/kaiforward-native && test -r /init.kaiforward-native.rc && test -e /dev/tun')
    return adb


# These commands run on the phone, including recovery if the PC disconnects.
# No private login state is backed up to the PC or included in a payload.
COMMON = r'''#!/system/bin/sh
set -eu
PATH=/system/bin:/system/xbin
export PATH
umask 077
runtime=/data/kaiforward/tailscale
startup=/system/kaiforward/tailscale.sh
boot=/dev/block/bootdevice/by-name/boot
alive_pid() {
    [ -r "/proc/$1/stat" ] &&
        [ "$(busybox awk '{print $3}' "/proc/$1/stat" 2>/dev/null)" != Z ] &&
        kill -0 "$1" 2>/dev/null
}
stop_pid() {
    [ -r "$1" ] || return 0
    pid=$(cat "$1")
    case "$pid" in ''|*[!0-9]*) echo 'Invalid saved Tailscale PID' >&2; return 1 ;; esac
    alive_pid "$pid" || return 0
    busybox tr '\000' ' ' <"/proc/$pid/cmdline" | busybox grep -F -q "$2" || {
        echo 'Saved PID belongs to another process; refusing to stop it.' >&2; return 1;
    }
    kill "$pid" 2>/dev/null || true
    tries=0
    while alive_pid "$pid"; do
        tries=$((tries + 1))
        [ "$tries" -le 20 ] || { echo 'Tailscale did not stop' >&2; return 1; }
        sleep 1
    done
}
stop_runtime() {
    if [ -n "$(getprop init.svc.kf-tailscale)" ]; then
        stop kf-tailscale
    fi
    stop_pid "$runtime/supervisor.pid" "$startup"
    stop_pid "$runtime/tailscaled.pid" "$runtime/tailscaled"
}
start_runtime() {
    [ -x "$runtime/tailscaled" ] && [ -f "$startup" ] || return 0
    if [ -n "$(getprop init.svc.kf-tailscale)" ]; then
        start kf-tailscale
    else
        busybox nohup /system/bin/sh "$startup" >"$runtime/service.log" 2>&1 </dev/null &
        echo $! >"$runtime/supervisor.pid"
    fi
}
restore_files() {
    if [ -s "$bundle/previous.SHA256SUMS" ]; then
        (cd "$bundle/previous" && busybox sha256sum -c "$bundle/previous.SHA256SUMS") || return 1
    fi
    while read -r name target mode; do
        if [ -f "$bundle/previous/$name" ]; then
            busybox cp -p "$bundle/previous/$name" "$target.restore" || return 1
            busybox mv "$target.restore" "$target" || return 1
        else
            busybox rm -f "$target" || return 1
        fi
    done < "$bundle/files"
}
install_files() {
    while read -r name target mode; do
        busybox cp "$bundle/$name" "$target.new" || return 1
        busybox chmod "$mode" "$target.new" || return 1
        busybox mv "$target.new" "$target" || return 1
    done < "$bundle/files"
}
restore_boot() {
    expected=$(busybox sha256sum "$bundle/boot-before.img") || return 1
    [ "${expected%% *}" = '@ORIGINAL@' ] || return 1
    busybox dd if="$bundle/boot-before.img" of="$boot" bs=1048576 || return 1
    sync
    actual=$(busybox sha256sum "$boot") || return 1
    [ "${actual%% *}" = '@ORIGINAL@' ]
}
'''

APPLY = r'''
bundle=@BUNDLE@
lock=/data/kaiforward/tailscale-install.lock
locked=0
remounted=0
stopped=0
changed=0
boot_written=0
complete=0
cleanup() {
    rc=$?
    trap - EXIT HUP INT TERM
    if [ "$complete" = 0 ]; then
        if [ "$changed" = 1 ]; then
            echo 'Installation failed; restoring the previous runtime.' >&2
            stop_runtime || rc=1
            restore_files || rc=1
        fi
        if [ "$boot_written" = 1 ]; then restore_boot || rc=1; fi
        if [ "$stopped" = 1 ]; then start_runtime || rc=1; fi
    fi
    if [ "$remounted" = 1 ]; then busybox mount -o remount,ro /system || rc=1; fi
    if [ "$locked" = 1 ]; then busybox rmdir "$lock" || rc=1; fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
busybox mkdir "$lock"
locked=1
cd "$bundle"
busybox sha256sum -c SHA256SUMS
current=$(busybox sha256sum "$boot")
[ "${current%% *}" = '@ORIGINAL@' ]
[ "$(blockdev --getsize64 "$boot")" = @SIZE@ ]
[ "$(busybox stat -c %s "$bundle/boot-installed.img")" = @SIZE@ ]
/system/bin/sh -n "$bundle/startup.sh"
busybox chmod 700 "$bundle/tailscale"
[ "$("$bundle/tailscale" version | busybox head -1)" = '@VERSION@' ]
busybox mkdir "$bundle/previous"
while read -r name target mode; do
    if [ -e "$target" ]; then
        [ -f "$target" ] && [ ! -L "$target" ]
        busybox cp -p "$target" "$bundle/previous/$name"
    fi
done < "$bundle/files"
: > "$bundle/previous.SHA256SUMS"
while read -r name target mode; do
    if [ -f "$bundle/previous/$name" ]; then
        (cd "$bundle/previous" && busybox sha256sum "$name") >> "$bundle/previous.SHA256SUMS"
    fi
done < "$bundle/files"
options=$(busybox awk '$2 == "/system" {print $4}' /proc/mounts)
case ",$options," in
    *,ro,*) busybox mount -o remount,rw /system; remounted=1 ;;
    *,rw,*) ;;
    *) echo 'Cannot determine /system mount mode' >&2; exit 1 ;;
esac
busybox mkdir -p "$runtime"
busybox chmod 700 "$runtime"
stopped=1
stop_runtime
changed=1
install_files
sync
if [ '@ORIGINAL@' != '@PATCHED@' ]; then
    boot_written=1
    busybox dd if="$bundle/boot-installed.img" of="$boot" bs=1048576
    sync
fi
actual=$(busybox sha256sum "$boot")
[ "${actual%% *}" = '@PATCHED@' ]
while read -r name target mode; do
    expected=$(busybox sha256sum "$bundle/$name")
    actual=$(busybox sha256sum "$target")
    [ "${actual%% *}" = "${expected%% *}" ]
done < "$bundle/files"
start_runtime
touch "$bundle/applied"
complete=1
echo 'Tailscale installation applied. No reboot performed.'
'''

ROLLBACK = r'''
bundle=@BUNDLE@
lock=/data/kaiforward/tailscale-install.lock
locked=0
remounted=0
stopped=0
changed=0
boot_written=0
complete=0
cleanup() {
    rc=$?
    trap - EXIT HUP INT TERM
    if [ "$complete" = 0 ]; then
        if [ "$changed" = 1 ]; then
            echo 'Rollback failed; recovering the installed runtime.' >&2
            stop_runtime || rc=1
            install_files || rc=1
        fi
        if [ "$boot_written" = 1 ]; then
            busybox dd if="$bundle/boot-installed.img" of="$boot" bs=1048576 || rc=1
            sync
            actual=$(busybox sha256sum "$boot") || rc=1
            [ "${actual%% *}" = '@PATCHED@' ] || rc=1
        fi
        if [ "$stopped" = 1 ]; then start_runtime || rc=1; fi
    fi
    if [ "$remounted" = 1 ]; then busybox mount -o remount,ro /system || rc=1; fi
    if [ "$locked" = 1 ]; then busybox rmdir "$lock" || rc=1; fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
busybox mkdir "$lock"
locked=1
[ -f "$bundle/applied" ] && [ ! -f "$bundle/rolled-back" ]
cd "$bundle"
busybox sha256sum -c SHA256SUMS
if [ -s "$bundle/previous.SHA256SUMS" ]; then
    (cd "$bundle/previous" && busybox sha256sum -c "$bundle/previous.SHA256SUMS")
fi
current=$(busybox sha256sum "$boot")
[ "${current%% *}" = '@PATCHED@' ] || {
    echo 'Boot changed since installation; refusing to overwrite newer firmware.' >&2; exit 1;
}
while read -r name target mode; do
    expected=$(busybox sha256sum "$bundle/$name")
    actual=$(busybox sha256sum "$target")
    [ "${actual%% *}" = "${expected%% *}" ] || {
        echo 'Installed files changed; refusing an out-of-order rollback.' >&2; exit 1;
    }
done < "$bundle/files"
options=$(busybox awk '$2 == "/system" {print $4}' /proc/mounts)
case ",$options," in
    *,ro,*) busybox mount -o remount,rw /system; remounted=1 ;;
    *,rw,*) ;;
    *) exit 1 ;;
esac
stopped=1
stop_runtime
changed=1
restore_files
if [ '@ORIGINAL@' != '@PATCHED@' ]; then boot_written=1; restore_boot; fi
start_runtime
touch "$bundle/rolled-back"
complete=1
echo 'Previous boot and Tailscale runtime restored; login state retained. No reboot performed.'
'''


def bundle_scripts(bundle, manifest, version=None):
    substitutions = {'@BUNDLE@': shlex.quote(bundle), '@ORIGINAL@': manifest['original_sha256'],
                     '@PATCHED@': manifest['patched_sha256'], '@SIZE@': str(manifest['bytes']),
                     '@VERSION@': version or inputs()['version']}
    result = {}
    for name, body in [('apply.sh', APPLY), ('rollback.sh', ROLLBACK)]:
        text = COMMON + body
        for placeholder, value in substitutions.items():
            text = text.replace(placeholder, value)
        result[name] = text
    return result


def wait_ready(adb, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(adb.shell(ts_command('status --json'), timeout=5))
        except (AdbError, json.JSONDecodeError, subprocess.TimeoutExpired):
            time.sleep(1)
    raise RuntimeError('Tailscale did not start. Check Wi-Fi and /data/kaiforward/tailscale/service.log; no reboot was performed.')


def ts_command(arguments):
    return RUNTIME + '/tailscale --socket=' + RUNTIME + '/tailscaled.sock ' + arguments


def login(adb):
    failure = None
    try:
        output = adb.shell(ts_command('up --accept-dns=false --accept-routes=false --netfilter-mode=off '
                                      '--hostname=nokia8110 --timeout=10s'), timeout=20)
        if output.strip():
            print(output.strip())
    except AdbError as error:
        failure = error
    state = json.loads(adb.shell(ts_command('status --json')))
    adb.shell(ts_command('set --auto-update=false'))
    if state.get('AuthURL'):
        print('Open this link to join your tailnet: ' + state['AuthURL'])
    elif failure and state.get('BackendState') != 'Running':
        raise failure
    else:
        print('Tailscale state: ' + state.get('BackendState', 'unknown'))
    return state


def install(serial=None, cache=None, archive=None, ca_bundle=None):
    runtime, pinned = prepare(cache or PROJECT / 'data/tailscale-install', archive, ca_bundle)
    adb = phone(serial)
    install_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    remote = INSTALLS + '/' + install_id
    local = PROJECT / 'backups/tailscale' / (re.sub(r'[^\w.-]', '_', adb.serial) + '-' + install_id)
    local.mkdir(parents=True, mode=0o700)
    adb.shell('umask 077; mkdir -p ' + shlex.quote(remote) + ' && chmod 700 ' + shlex.quote(remote))
    adb.shell('busybox dd if=' + BOOT + ' of=' + shlex.quote(remote + '/boot-before.img') + ' bs=1048576')
    remote_hash = adb.shell('busybox sha256sum ' + shlex.quote(remote + '/boot-before.img')).split()[0]
    adb.run('pull', remote + '/boot-before.img', str(local / 'boot-before.img'))
    if file_sha256(local / 'boot-before.img') != remote_hash:
        raise ValueError('Boot backup transfer hash mismatch; nothing installed')
    patched, manifest = patch_boot((local / 'boot-before.img').read_bytes())
    desired = {name: file_sha256(runtime / name) for name in FILES if name != 'installation.json'}
    current = adb.shell('for target in ' + ' '.join(shlex.quote(FILES[name][0]) for name in desired) +
                        '; do if [ -f "$target" ]; then busybox sha256sum "$target"; fi; done')
    installed = {line.split(maxsplit=1)[1].strip(): line.split()[0] for line in current.splitlines()}
    if not manifest['changed'] and all(installed.get(FILES[name][0]) == digest for name, digest in desired.items()):
        print('Tailscale files and boot service are already installed; no files or boot partition rewritten.')
        print('Backup: ' + str(local / 'boot-before.img'))
        manage('start', adb=adb)
        login(adb)
        return {'changed': False, 'backup': str(local)}
    (local / 'boot-installed.img').write_bytes(patched)
    for name in desired:
        shutil.copyfile(runtime / name, local / name)
    metadata = dict(manifest, install_id=install_id, version=pinned['version'],
                    device=adb.serial, file_sha256=desired)
    (local / 'installation.json').write_text(json.dumps(metadata, indent=2) + '\n')
    (local / 'files').write_text(''.join(f'{name} {target} {mode}\n' for name, (target, mode) in FILES.items()))
    for name, content in bundle_scripts(remote, manifest, pinned['version']).items():
        (local / name).write_text(content)
    for name in ('apply.sh', 'rollback.sh'):
        subprocess.run(['sh', '-n', str(local / name)], check=True)
    names = [*FILES, 'files', 'boot-before.img', 'boot-installed.img', 'apply.sh', 'rollback.sh']
    (local / 'SHA256SUMS').write_text(''.join(file_sha256(local / name) + '  ' + name + '\n' for name in names))
    # Originals stay on the phone; none of the files pushed contain tailnet keys.
    for name in [n for n in names if n != 'boot-before.img'] + ['SHA256SUMS']:
        adb.run('push', str(local / name), remote + '/' + name)
    print('Boot change required: ' + ('yes' if manifest['changed'] else 'no'))
    print('Backup: ' + str(local / 'boot-before.img'))
    print('Rollback: python3 -m scripts.tailscale' + (' --serial ' + shlex.quote(adb.serial)) + ' rollback ' + install_id)
    print(adb.shell('/system/bin/sh ' + shlex.quote(remote + '/apply.sh'), timeout=180).strip())
    wait_ready(adb)
    login(adb)
    print('Boot startup is installed. Restart the phone yourself when ready; this installer never reboots.')
    return metadata


def manage(action, serial=None, adb=None, install_id=None):
    adb = adb or phone(serial)
    if action == 'status':
        service = adb.shell('getprop init.svc.kf-tailscale').strip()
        print('Boot service: ' + (service or 'not loaded in this boot; startup may be pending your next reboot'))
        print(adb.shell(ts_command('status')).strip())
    elif action == 'login':
        return login(adb)
    elif action in ('start', 'stop'):
        if action == 'start':
            adb.shell('test -x ' + RUNTIME + '/tailscaled && test -r ' + STARTUP)
        with tempfile.TemporaryDirectory(prefix='kaiforward-tailscale-') as directory:
            script = Path(directory) / 'manage.sh'
            # Starting is idempotent: a live supervisor already holds its lock.
            body = 'stop_runtime\n' if action == 'stop' else r'''
if [ -r "$runtime/supervisor.pid" ] && alive_pid "$(cat "$runtime/supervisor.pid")"; then
    busybox tr '\000' ' ' <"/proc/$(cat "$runtime/supervisor.pid")/cmdline" | busybox grep -F -q "$startup"
else
    start_runtime
fi
'''
            script.write_text(COMMON + body)
            remote = '/data/local/tmp/tailscale-manage-' + uuid.uuid4().hex + '.sh'
            try:
                adb.run('push', str(script), remote)
                print(adb.shell('/system/bin/sh ' + shlex.quote(remote), timeout=60).strip())
            finally:
                adb.shell('rm -f ' + shlex.quote(remote))
        if action == 'start':
            wait_ready(adb)
            print('Tailscale started; saved login and running preference retained.')
        else:
            print('Tailscale stopped for this session. Boot startup and saved login retained.')
    elif action == 'rollback':
        if not install_id or not INSTALL_ID.fullmatch(install_id):
            raise ValueError('Invalid installation ID')
        target = INSTALLS + '/' + install_id + '/rollback.sh'
        print(adb.shell('/system/bin/sh ' + shlex.quote(target), timeout=180).strip())
    else:
        raise ValueError('Unknown Tailscale action')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial', help='Select the connected phone')
    commands = parser.add_subparsers(dest='action', required=True)
    setup = commands.add_parser('install', help='Install the verified runtime and boot startup, without rebooting')
    setup.add_argument('--archive', type=Path, help='Use a local copy of the pinned ARM archive')
    setup.add_argument('--cache', type=Path, help='Download/build cache (default: data/tailscale-install)')
    setup.add_argument('--ca-bundle', type=Path, help='Public PEM CA bundle (default: host system bundle)')
    for action in ('status', 'login', 'start', 'stop'):
        commands.add_parser(action)
    undo = commands.add_parser('rollback', help='Restore the preceding boot/runtime while retaining login state')
    undo.add_argument('install_id')
    args = parser.parse_args()
    try:
        if args.action == 'install':
            install(args.serial, args.cache, args.archive, args.ca_bundle)
        else:
            manage(args.action, args.serial, install_id=getattr(args, 'install_id', None))
    except (AdbError, ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
