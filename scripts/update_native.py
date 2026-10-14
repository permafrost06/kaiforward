#!/usr/bin/env python3
"""Update native userspace over root ADB without flashing partitions or rebooting."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
import uuid

from scripts.adb import Adb
from scripts.files import file_sha256

PROJECT = Path(__file__).resolve().parents[1]
UPDATES = '/data/kaiforward/updates'
SERVICES = ('kf-permission', 'kaiforward-sms', 'kf-upload', 'kf-relay',
            'kaiforward-wifi', 'kaiforward-ui', 'kf-tailscale')
PAYLOAD = {
    'kaiforward-native': ('/system/bin/kaiforward-native', '755'),
    'wifi.sh': ('/system/kaiforward/wifi.sh', '755'),
    'tailscale.sh': ('/system/kaiforward/tailscale.sh', '755'),
    'ssh.sh': ('/system/kaiforward/ssh.sh', '755'),
    'udhcpc.sh': ('/system/kaiforward/udhcpc.sh', '755'),
    'dhcp-native.conf': ('/system/etc/dhcpcd/dhcpcd-hooks/99-kaiforward.conf', '644'),
    'FONT-LICENSE.txt': ('/system/kaiforward/FONT-LICENSE.txt', '644'),
}


def validate_runtime(runtime, relay=None):
    """Reject stale builds and wrong-architecture or damaged binaries before ADB."""
    files = {}
    builds = [(runtime, 'native-manifest.json', 'kaiforward-native')]
    if relay:
        builds.append((relay, 'relay-manifest.json', 'kaiforward-relay'))
    for directory, manifest_name, binary_name in builds:
        manifest = json.loads((directory / manifest_name).read_text())
        for name, expected in manifest['sources'].items():
            source = (PROJECT / name).resolve()
            if not source.is_relative_to(PROJECT) or file_sha256(source) != expected:
                raise ValueError('Sources changed since build; rebuild first: ' + name)
        binary = directory / binary_name
        header = binary.read_bytes()[:20]
        if header[:7] != b'\x7fELF\x01\x01\x01' or int.from_bytes(header[18:20], 'little') != 40:
            raise ValueError('Expected a 32-bit little-endian ARM binary: ' + str(binary))
        if file_sha256(binary) != manifest['binary_sha256']:
            raise ValueError('Binary does not match build manifest: ' + str(binary))
        if directory == runtime:
            for name in [*PAYLOAD, 'init.native.rc']:
                path = directory / name
                expected = (manifest['binary_sha256'] if name == binary_name else
                            manifest['sources']['firmware/native/' + name])
                if file_sha256(path) != expected:
                    raise ValueError('Runtime file does not match build manifest: ' + name)
                if name != 'init.native.rc':
                    files[name] = path
        else:
            files[binary_name] = binary
    return files, file_sha256(runtime / 'init.native.rc')


def check_phone(adb, init_hash=None):
    if adb.shell('id -u').strip() != '0':
        raise ValueError('Runtime updates need Gerda root ADB')
    adb.shell('test -x /system/bin/kaiforward-native && test -r /init.kaiforward-native.rc')
    if init_hash:
        actual = adb.shell('busybox sha256sum /init.kaiforward-native.rc').split()[0]
        if actual != init_hash:
            raise ValueError('Startup definitions differ; use firmware images for init changes')
    mount = adb.shell('busybox awk \'$2 == "/system" {print $4}\' /proc/mounts').strip()
    if not {'ro', 'rw'}.intersection(mount.split(',')):
        raise ValueError('Cannot determine /system mount mode')
    states = {name: adb.shell('getprop init.svc.' + name).strip() for name in SERVICES}
    if any(state not in ('running', 'stopped') for state in states.values()):
        raise ValueError('Native services missing or restarting: ' + json.dumps(states))
    return {'serial': adb.serial, 'system_mount': mount, 'services': states}


# Run the transaction on the phone, with cleanup for ordinary errors and signals.
# Every file replacement uses a sibling temporary file and rename: never overwrite
# the inode of a running executable. Backups remain available after success/failure.
COMMON = r'''#!/system/bin/sh
set -eu
PATH=/system/bin:/system/xbin
export PATH
umask 077
dir=@DIRECTORY@
lock=/data/kaiforward/update.lock
locked=0
remounted=0
stopped=0
changed=0
success=0
recovery="$dir/previous"
services="@SERVICES@"
fail() { echo "$*" >&2; return 1; }
was_running() {
    # BusyBox 1.20 on the phone does not reliably implement grep -x.
    while IFS= read -r saved; do
        [ "$saved" = "$1" ] && return 0
    done < "$dir/running-services"
    return 1
}
wait_state() {
    service=$1
    desired=$2
    count=0
    while [ "$(getprop "init.svc.$service")" != "$desired" ]; do
        count=$((count + 1))
        [ "$count" -le 15 ] || { fail "$service did not become $desired"; return 1; }
        sleep 1
    done
}
stop_services() {
    # Stop dependents first; keep the permission controller until last.
    for service in @STOP_SERVICES@; do
        if was_running "$service"; then
            setprop ctl.stop "$service" || return 1
            wait_state "$service" stopped || return 1
        fi
    done
}
start_services() {
    for service in $services; do
        if was_running "$service"; then
            setprop ctl.start "$service" || return 1
            wait_state "$service" running || return 1
        fi
    done
}
check_services() {
    # A process can briefly report running before crashing on startup.
    sleep 2
    for service in $services; do
        if was_running "$service"; then
            [ "$(getprop "init.svc.$service")" = running ] || {
                fail "$service failed its startup check"; return 1;
            }
        fi
    done
}
check_processes() {
    runtime=$1
    for executable in /proc/[0-9]*/exe; do
        target=$(busybox readlink "$executable") || continue
        case "$target" in
            /system/bin/kaiforward-native*) binary=kaiforward-native ;;
            /system/bin/kaiforward-relay*) binary=kaiforward-relay ;;
            *) continue ;;
        esac
        [ -f "$runtime/$binary" ] || continue
        # Short-lived Wi-Fi helpers can exit between readlink and hashing.
        actual=$(busybox sha256sum "$executable" 2>/dev/null) || continue
        expected=$(busybox sha256sum "$runtime/$binary") || return 1
        [ "${actual%% *}" = "${expected%% *}" ] || {
            fail "Running process still uses another binary: $executable"; return 1;
        }
    done
}
verify_backup() {
    (cd "$1" && busybox sha256sum -c SHA256SUMS) || return 1
}
replace_files() {
    source=$1
    while read -r name target mode; do
        temporary="$target.kf-update"
        busybox cp -p "$source/$name" "$temporary" || return 1
        if [ "$source" = "$dir" ]; then
            busybox chown 0:0 "$temporary" || return 1
            busybox chmod "$mode" "$temporary" || return 1
        fi
        busybox mv -f "$temporary" "$target" || return 1
        if command -v restorecon >/dev/null 2>&1; then
            restorecon "$target" || return 1
        fi
    done < "$dir/files"
    busybox sync || return 1
}
verify_installed() {
    source=$1
    while read -r name target mode; do
        expected=$(busybox sha256sum "$source/$name") || return 1
        actual=$(busybox sha256sum "$target") || return 1
        [ "${expected%% *}" = "${actual%% *}" ] || {
            fail "Installed hash mismatch: $target"; return 1;
        }
    done < "$dir/files"
}
cleanup() {
    rc=$?
    trap - EXIT HUP INT TERM
    if [ "$success" = 0 ] && [ "$changed" = 1 ]; then
        echo 'Update failed; restoring previous runtime.' >&2
        if verify_backup "$recovery" && stop_services && replace_files "$recovery" &&
            verify_installed "$recovery" && start_services && check_services && check_processes "$recovery"; then
            echo 'Previous runtime restored.' >&2
            stopped=0
        else
            echo "Recovery failed; recovery files retained at $recovery" >&2
            rc=1
        fi
    elif [ "$success" = 0 ] && [ "$stopped" = 1 ]; then
        start_services || rc=1
    fi
    if [ "$remounted" = 1 ]; then
        busybox mount -o remount,ro /system || rc=1
    fi
    if [ "$locked" = 1 ]; then
        busybox rmdir "$lock" || rc=1
    fi
    exit "$rc"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
[ "$(id -u)" = 0 ] || fail 'Root ADB required'
busybox mkdir "$lock" || fail 'Another update is active (or an interrupted update left the lock)'
locked=1
cd "$dir"
busybox sha256sum -c SHA256SUMS
actual=$(busybox sha256sum /init.kaiforward-native.rc)
[ "${actual%% *}" = '@INIT_HASH@' ] || fail 'Startup definitions differ; flash images for init changes'
: > "$dir/running-services"
for service in $services; do
    state=$(getprop "init.svc.$service")
    case "$state" in
        running) echo "$service" >> "$dir/running-services" ;;
        stopped) ;;
        *) fail "Service missing or restarting: $service ($state)" ;;
    esac
done
options=$(busybox awk '$2 == "/system" {print $4}' /proc/mounts)
case ",$options," in
    *,ro,*) remounted=1; busybox mount -o remount,rw /system ;;
    *,rw,*) ;;
    *) fail 'Cannot determine /system mount mode' ;;
esac
'''

APPLY = r'''
busybox chmod 755 "$dir/kaiforward-native"
[ "$("$dir/kaiforward-native" --network-prefix 192.0.2.1 255.255.255.0)" = '192.0.2.0/24' ] ||
    fail 'Native runtime failed ARM execution check'
busybox mkdir "$dir/previous"
while read -r name target mode; do
    busybox cp -p "$target" "$dir/previous/$name"
done < "$dir/files"
(cd "$dir/previous" && busybox sha256sum @PAYLOAD_NAMES@ > SHA256SUMS)
verify_backup "$dir/previous"
stopped=1
stop_services
changed=1
replace_files "$dir"
verify_installed "$dir"
start_services
check_services
check_processes "$dir"
success=1
echo "Runtime updated. Rollback ID: @UPDATE_ID@"
'''

ROLLBACK = r'''
verify_backup "$dir/previous"
# Keep the current runtime intact if the requested rollback fails.
recovery="$dir/before-rollback"
busybox mkdir -p "$recovery"
while read -r name target mode; do
    busybox cp -p "$target" "$recovery/$name"
done < "$dir/files"
(cd "$recovery" && busybox sha256sum @PAYLOAD_NAMES@ > SHA256SUMS)
verify_backup "$recovery"
stopped=1
stop_services
changed=1
replace_files "$dir/previous"
verify_installed "$dir/previous"
start_services
check_services
check_processes "$dir/previous"
success=1
echo "Previous runtime restored: @UPDATE_ID@"
'''


def bundle_scripts(update_id, names, init_hash):
    services = list(SERVICES)
    # SMS owns call control; restarting it requires reconnecting the relay too.
    replacements = {
        '@DIRECTORY@': shlex.quote(UPDATES + '/' + update_id),
        '@SERVICES@': ' '.join(services),
        '@STOP_SERVICES@': ' '.join(reversed(services)),
        '@INIT_HASH@': init_hash,
        '@PAYLOAD_NAMES@': ' '.join(shlex.quote(name) for name in names),
        '@UPDATE_ID@': update_id,
    }
    scripts = {}
    for name, body in [('apply.sh', APPLY), ('rollback.sh', ROLLBACK)]:
        script = COMMON + body
        for key, value in replacements.items():
            script = script.replace(key, value)
        scripts[name] = script
    return scripts


def install(runtime, relay=None, serial=None):
    files, init_hash = validate_runtime(runtime, relay)
    adb = Adb(serial)
    phone = check_phone(adb, init_hash)
    update_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    remote = UPDATES + '/' + update_id
    plan = dict(PAYLOAD)
    if relay:
        plan['kaiforward-relay'] = ('/system/bin/kaiforward-relay', '755')
    with tempfile.TemporaryDirectory(prefix='native-update-') as tmp:
        root = Path(tmp)
        for name, content in bundle_scripts(update_id, files, init_hash).items():
            (root / name).write_text(content)
        (root / 'files').write_text(''.join(f'{name} {target} {mode}\n' for name, (target, mode) in plan.items()))
        metadata = dict(phone, update_id=update_id, files={name: file_sha256(path) for name, path in files.items()})
        (root / 'update.json').write_text(json.dumps(metadata, indent=2) + '\n')
        transfer = {**files, **{path.name: path for path in root.iterdir()}}
        sums = root / 'SHA256SUMS'
        sums.write_text(''.join(file_sha256(path) + '  ' + name + '\n' for name, path in transfer.items()))
        transfer[sums.name] = sums
        adb.shell('mkdir -p ' + UPDATES + ' && chmod 700 ' + UPDATES + ' && mkdir ' + remote)
        for name, path in transfer.items():
            adb.run('push', str(path), remote + '/' + name)
        # Check the entire bundle before executing any script from it.
        adb.shell('cd ' + remote + ' && busybox sha256sum -c SHA256SUMS')
        result = adb.shell('/system/bin/sh ' + remote + '/apply.sh', timeout=300)
    print(result.strip())
    print('Rollback: python3 -m scripts.update_native --serial ' + shlex.quote(adb.serial) + ' --rollback ' + update_id)
    return update_id


def rollback(update_id, serial=None):
    if not re.fullmatch(r'\d{8}-\d{6}-[0-9a-f]{8}', update_id):
        raise ValueError('Invalid rollback ID')
    adb = Adb(serial)
    check_phone(adb)
    remote = UPDATES + '/' + update_id
    adb.shell('cd ' + remote + ' && busybox sha256sum -c SHA256SUMS')
    print(adb.shell('/system/bin/sh ' + remote + '/rollback.sh', timeout=300).strip())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serial')
    parser.add_argument('--runtime', type=Path, default=PROJECT / 'dist/native-runtime')
    parser.add_argument('--relay-runtime', type=Path, help='Also install a rebuilt call relay')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--check', action='store_true', help='Read-only root ADB and service compatibility check')
    action.add_argument('--rollback', metavar='UPDATE_ID', help='Restore files saved by an earlier update')
    action.add_argument('--build', action='store_true', help='Build the native runtime, then push and restart it')
    parser.add_argument('--build-root', type=Path, default=PROJECT / 'data/native-build')
    parser.add_argument('--zig', type=Path)
    parser.add_argument('--sqlite', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.check:
        init_hash = file_sha256(PROJECT / 'firmware/native/init.native.rc')
        print(json.dumps(check_phone(Adb(args.serial), init_hash), indent=2))
    elif args.rollback:
        rollback(args.rollback, args.serial)
    else:
        if args.build:
            from firmware.scripts.build_native import build
            build(args.build_root.resolve(), args.runtime.resolve(), args.zig, args.sqlite)
        install(args.runtime.resolve(), args.relay_runtime.resolve() if args.relay_runtime else None, args.serial)


if __name__ == '__main__':
    main()
