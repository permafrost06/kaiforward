"""Back up every eMMC partition of a rooted Nokia 8110 4G over ADB.

Nothing on the phone's block devices is written. Each partition is read once
on the phone and streamed to the PC while the phone hashes exactly the bytes it
sends; the PC copy is accepted only when both SHA-256 digests and the expected
size agree.
"""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import subprocess
import sys
import zlib

from scripts.adb import Adb, AdbError
from scripts import root_shell

EXPECTED_PRODUCT = 'Nokia 8110 4G'
BY_NAME = '/dev/block/bootdevice/by-name'
SECTOR = 512
GPT_PRIMARY_SECTORS = 34   # protective MBR, header and 32 sectors of entries
GPT_BACKUP_SECTORS = 33    # 32 sectors of entries and the backup header
CHUNK = 1 << 20
MANIFEST_VERSION = 1


class BackupError(RuntimeError):
    pass


def parse_by_name(listing):
    """Map partition names to kernel block names from `ls -l` of by-name."""
    partitions = {}
    for name, block in re.findall(r'\s(\w+) -> /dev/block/(mmcblk\d+p\d+)\s*$', listing, re.M):
        partitions[name] = block
    if not partitions:
        raise BackupError('No partitions found in ' + BY_NAME)
    return partitions


def parse_mounts(text):
    """Return {block device: (mount point, options)} for mounted partitions."""
    mounts = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 4:
            device = re.sub(r'^' + re.escape(BY_NAME) + r'/', '', fields[0])
            mounts[device.rsplit('/', 1)[-1]] = (fields[1], fields[3].split(','))
    return mounts


def describe_device(adb, require_root=True):
    """Collect the facts the backup depends on and refuse unexpected devices."""
    if require_root and adb.shell('busybox id -u').strip() != '0':
        raise BackupError('Phone commands are not running as root')
    product = adb.shell('getprop ro.product.device').strip()
    if product != EXPECTED_PRODUCT:
        raise BackupError('Unexpected device: ' + repr(product))
    disk = adb.shell('cd /sys/block/mmcblk0 && cat queue/logical_block_size size').split()
    if disk[0] != str(SECTOR):
        raise BackupError('Unexpected logical sector size ' + disk[0])
    names = parse_by_name(adb.shell('ls -l ' + BY_NAME))
    blocks = ' '.join(sorted(set(names.values())))
    geometry = adb.shell('for b in ' + blocks + '; do echo $b $(cat /sys/class/block/$b/start /sys/class/block/$b/size); done')
    extents = {}
    for line in geometry.splitlines():
        block, start, size = line.split()
        extents[block] = (int(start), int(size))
    mounts = parse_mounts(adb.shell('cat /proc/mounts'))
    partitions = {}
    for name, block in sorted(names.items()):
        start, size = extents[block]
        entry = {'block': block, 'start_bytes': start * SECTOR, 'bytes': size * SECTOR}
        if name in mounts or block in mounts:
            point, options = mounts.get(name) or mounts[block]
            entry['mounted'] = point
            entry['live'] = 'rw' in options
        partitions[name] = entry
    return {
        'product': product,
        'serial': adb.shell('getprop ro.serialno').strip(),
        'build': adb.shell('getprop ro.build.version.incremental').strip(),
        'kernel': adb.shell('uname -a').strip(),
        'disk_bytes': int(disk[1]) * SECTOR,
        'partitions': partitions,
    }


def regions(device):
    """List the regions to copy: the two GPT copies, then each partition."""
    disk_sectors = device['disk_bytes'] // SECTOR
    items = [
        ('gpt-primary', '/dev/block/mmcblk0', 0, GPT_PRIMARY_SECTORS),
        ('gpt-backup', '/dev/block/mmcblk0', disk_sectors - GPT_BACKUP_SECTORS, GPT_BACKUP_SECTORS),
    ]
    for name, entry in device['partitions'].items():
        items.append((name, BY_NAME + '/' + name, None, entry['bytes'] // SECTOR))
    return items


def stream_command(source, skip, count, workdir):
    """Phone command that writes the region to stdout and its hash to workdir.

    This adbd merges stderr into exec-out, so nothing else may reach stdout.
    """
    if skip is None:
        dd = 'busybox dd if=' + source + ' bs=1048576'
    else:
        dd = 'busybox dd if={} bs={} skip={} count={}'.format(source, SECTOR, skip, count)
    return ('exec 2>{w}/err; busybox mkfifo {w}/fifo && '
            '{{ busybox sha256sum < {w}/fifo > {w}/sum & }} && '
            '{dd} 2>{w}/dd | busybox tee {w}/fifo; wait').format(w=workdir, dd=dd)


def copy_region(adb, name, source, skip, count, destination, log=print):
    expected = count * SECTOR
    partial = destination.with_name(destination.name + '.partial')
    workdir = adb.shell('busybox mktemp -d /dev/kfbackup.XXXXXX').strip()
    if not re.fullmatch(r'/dev/kfbackup\.\w+', workdir):
        raise BackupError('Unable to create phone work directory: ' + workdir)
    try:
        digest = hashlib.sha256()
        received = 0
        with partial.open('wb') as output, adb.exec_out(stream_command(source, skip, count, workdir)) as stream:
            while chunk := stream.stdout.read(CHUNK):
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if received % (64 * CHUNK) < len(chunk):
                    log('  {} {:.0f}%'.format(name, 100 * received / expected))
            output.flush()
            os.fsync(output.fileno())
        if stream.returncode:
            raise BackupError('adb exited with {} while copying {}'.format(stream.returncode, name))
        phone = adb.shell('cat {w}/sum; echo; cat {w}/dd {w}/err'.format(w=workdir))
        match = re.match(r'([0-9a-f]{64})\s+-', phone)
        if not match:
            raise BackupError('Phone did not report a hash for {}: {}'.format(name, phone.strip()))
        if received != expected:
            raise BackupError('{}: received {} bytes, expected {}'.format(name, received, expected))
        if match[1] != digest.hexdigest():
            raise BackupError('{}: phone hash {} != PC hash {}'.format(name, match[1], digest.hexdigest()))
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    finally:
        adb.shell('rm -rf ' + workdir)
    partial.rename(destination)
    return {'file': destination.name, 'bytes': received, 'sha256': digest.hexdigest()}


def file_sha256(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


# Harmless e2fsck findings: this phone's formatter leaves the inode bitmap
# padding unset, and a mounted filesystem only writes its free/directory
# summary counters at unmount, so live copies always have stale totals.
BENIGN_EXT4_PROBLEMS = (
    'Padding at end of inode bitmap is not set.',
    'Free blocks count wrong',
    'Free inodes count wrong',
    'Directories count wrong for group',
)


def e2fsck_problems(output):
    """Return each problem e2fsck fixed, joined with a description on the previous line."""
    problems, previous = [], ''
    for line in output.splitlines():
        line = line.strip()
        if line.endswith('? yes'):
            text = line[:-len(' yes')]
            problems.append(previous + ' ' + text if text == 'Fix?' else text)
        if line:
            previous = line
    return problems


def check_ext4(image):
    """Replay the journal and fsck a disposable copy; None when not ext2/3/4.

    The image itself is never modified. `recoverable` is false only when
    e2fsck could not repair the copy.
    """
    if not shutil.which('e2fsck'):
        return None
    with image.open('rb') as source:
        source.seek(1024 + 56)
        if source.read(2) != b'\x53\xef':
            return None
    scratch = image.with_name(image.name + '.fsck')
    try:
        subprocess.run(['cp', '--reflink=auto', '--sparse=always', str(image), str(scratch)], check=True)
        result = subprocess.run(['e2fsck', '-fy', str(scratch)], capture_output=True, text=True)
    finally:
        scratch.unlink(missing_ok=True)
    problems = e2fsck_problems(result.stdout)
    unexpected = [line for line in problems if not line.startswith(BENIGN_EXT4_PROBLEMS)]
    return {'exit_code': result.returncode, 'recoverable': result.returncode < 4,
            'clean': result.returncode < 4 and not unexpected, 'problems': problems}


def parse_gpt_header(sector):
    if sector[:8] != b'EFI PART':
        raise BackupError('Missing GPT signature')
    size, crc = struct.unpack_from('<II', sector, 12)
    if zlib.crc32(sector[:16] + b'\0\0\0\0' + sector[20:size]) != crc:
        raise BackupError('GPT header CRC mismatch')
    count, entry_size, entries_crc = struct.unpack_from('<III', sector, 80)
    return count, entry_size, entries_crc


def check_gpt(primary, backup_copy, layout):
    """Verify both GPT copies and that they describe the kernel's partition layout."""
    header = parse_gpt_header(primary[SECTOR:2 * SECTOR])
    count, entry_size, crc = header
    entries = primary[2 * SECTOR:2 * SECTOR + count * entry_size]
    if zlib.crc32(entries) != crc or parse_gpt_header(backup_copy[-SECTOR:]) != header \
            or backup_copy[:len(entries)] != entries:
        raise BackupError('GPT entry arrays are inconsistent')
    table = {}
    for offset in range(0, len(entries), entry_size):
        first, last = struct.unpack_from('<QQ', entries, offset + 32)
        if first:
            name = entries[offset + 56:offset + 128].decode('utf-16-le').rstrip('\0')
            table[name] = {'start_bytes': first * SECTOR, 'bytes': (last - first + 1) * SECTOR}
    expected = {name: {'start_bytes': entry['start_bytes'], 'bytes': entry['bytes']} for name, entry in layout.items()}
    if table != expected:
        raise BackupError('GPT does not match the kernel partition layout')
    return len(table)


def write_manifest(destination, manifest):
    temporary = destination / 'manifest.json.tmp'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(destination / 'manifest.json')


def backup(adb, destination, resume=False, only=None, log=print):
    os.umask(0o077)
    device = describe_device(adb)
    adb.shell('rm -rf /dev/kfbackup.*')  # work directories left by a killed run
    plan = regions(device)
    if only:
        unknown = set(only) - {name for name, *_ in plan}
        if unknown:
            raise BackupError('Unknown partitions: {}. Use --list to see them.'.format(', '.join(sorted(unknown))))
        plan = [item for item in plan if item[0] in only]
    if resume:
        manifest = json.loads((destination / 'manifest.json').read_text())
        if manifest['device']['serial'] != device['serial'] or manifest['layout'] != device['partitions']:
            raise BackupError('Device or partition layout differs from the backup being resumed')
        for name, entry in manifest['images'].items():
            image = destination / entry['file']
            if image.stat().st_size != entry['bytes'] or file_sha256(image) != entry['sha256']:
                raise BackupError('Saved image failed verification: ' + name)
            log('Reverified ' + name)
    else:
        destination.mkdir(parents=True)
        manifest = {
            'version': MANIFEST_VERSION,
            'created': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            'device': {key: device[key] for key in ('product', 'serial', 'build', 'kernel', 'disk_bytes')},
            'layout': device['partitions'],
            'images': {},
        }
        (destination / 'partitions.txt').write_text(adb.shell('cat /proc/partitions; ls -l ' + BY_NAME))
        (destination / 'mounts.txt').write_text(adb.shell('cat /proc/mounts'))
        (destination / 'getprop.txt').write_text(adb.shell('getprop'))
        write_manifest(destination, manifest)
    needed = sum(count * SECTOR for name, _, _, count in plan if name not in manifest['images'])
    free = shutil.disk_usage(destination).free
    if needed > free:
        raise BackupError('Need {} bytes free at {}, have {}'.format(needed, destination, free))
    for name, source, skip, count in plan:
        if name in manifest['images']:
            continue
        layout = device['partitions'].get(name, {})
        log('Copying {} ({} bytes){}'.format(name, count * SECTOR, ' [live, mounted rw]' if layout.get('live') else ''))
        if layout.get('live'):
            adb.shell('sync')
        entry = copy_region(adb, name, source, skip, count, destination / (name + '.img'), log)
        if layout.get('live'):
            entry['live'] = True
            entry['ext4_check'] = check_ext4(destination / entry['file'])
        manifest['images'][name] = entry
        write_manifest(destination, manifest)
    if not only:
        manifest['gpt_partitions'] = check_gpt((destination / 'gpt-primary.img').read_bytes(),
                                               (destination / 'gpt-backup.img').read_bytes(), device['partitions'])
        write_manifest(destination, manifest)
    (destination / 'SHA256SUMS').write_text(''.join(
        '{}  {}\n'.format(entry['sha256'], entry['file']) for _, entry in sorted(manifest['images'].items())))
    return manifest


def connect(adb, transport):
    """Return a root command channel: root adbd, or the stock-firmware root shell."""
    if transport in ('auto', 'adb') and adb.shell('busybox id -u').strip() == '0':
        return adb
    if transport in ('auto', 'root-shell') and root_shell.available(adb):
        return root_shell.RootShell(adb)
    raise BackupError('No root access. On stock firmware run `python3 -m scripts.access` '
                      'and press the button on the phone first.')


def list_partitions(adb):
    """Print the regions a backup copies; reading the layout needs no root."""
    device = describe_device(adb, require_root=False)
    print('{:<14} {:>14}  {}'.format('NAME', 'BYTES', 'NOTE'))
    for name, _, _, count in regions(device):
        entry = device['partitions'].get(name, {})
        note = 'partition table' if name.startswith('gpt-') else ''
        if entry.get('mounted'):
            note = 'mounted {} {}'.format(entry['mounted'], 'read-write (copied live)' if entry['live'] else 'read-only')
        print('{:<14} {:>14,}  {}'.format(name, count * SECTOR, note))


def interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    signal.signal(signal.SIGTERM, interrupt)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('partitions', nargs='*', metavar='PARTITION',
                        help='Space-separated partitions to copy (default: all partitions and both GPT copies)')
    parser.add_argument('--list', action='store_true', help='List the partitions that can be backed up')
    parser.add_argument('--serial', help='ADB serial (default: the only connected device)')
    parser.add_argument('--output', type=Path, help='Backup directory (default: backups/<serial>-<timestamp>)')
    parser.add_argument('--resume', type=Path, help='Continue an interrupted backup after reverifying saved images')
    parser.add_argument('--transport', choices=('auto', 'adb', 'root-shell'), default='auto',
                        help='Root adbd (custom firmware) or the temporary root shell (stock)')
    args = parser.parse_args(argv)
    args.partitions = [name for argument in args.partitions for name in argument.split()]
    adb = Adb(args.serial)
    log = lambda text: print(text, flush=True)
    if args.list:
        try:
            list_partitions(adb)
        except (BackupError, AdbError) as error:
            print('Listing failed: {}'.format(error), file=sys.stderr)
            return 1
        return 0
    if args.resume:
        destination = args.resume
    else:
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        destination = args.output or Path('backups') / '{}-{}'.format(adb.serial, stamp)
    try:
        phone = connect(adb, args.transport)
        log('Using ' + ('root adbd' if phone is adb else 'the temporary root shell'))
        manifest = backup(phone, destination, bool(args.resume), args.partitions,
                          log=log)
    except (BackupError, AdbError) as error:
        print('Backup failed: {}'.format(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        if (destination / 'manifest.json').exists():
            print('Interrupted. Continue with --resume {}'.format(destination), file=sys.stderr)
        return 130
    live = [name for name, entry in manifest['images'].items() if entry.get('live')]
    unclean = [name for name in live if (manifest['images'][name].get('ext4_check') or {}).get('clean') is False]
    print('Verified {} images in {}'.format(len(manifest['images']), destination))
    if live:
        print('Copied while mounted read-write: ' + ', '.join(live))
    if unclean:
        print('ext4 check reported problems in live copies: ' + ', '.join(unclean))
    return 0


if __name__ == '__main__':
    sys.exit(main())
