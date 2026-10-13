"""Build a signed stock-recovery package that flashes boot/system from microSD.

The package is small: the images stay on the card next to it, and the
package carries their sizes and SHA-256 hashes. In recovery it refuses to
write anything unless every image matches its hash and every target
partition has the expected size and is not mounted. It then writes system
before boot and checks each partition's readback hash. Recovery, radio,
bootloader and data partitions are never written.

Stock recovery accepts packages signed with the AOSP test key. The recovery
updater, busybox and key are in recovery/; see recovery/README.md.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import zipfile

from scripts.adb import Adb, AdbError
from scripts.backup import EXPECTED_PRODUCT, file_sha256

# Written in this order: boot decides what runs, so it changes last.
PARTITIONS = ('system', 'boot')
CARD_DIRECTORY = 'kaiforward'
CARD = '/storage/sdcard'  # the microSD card as stock KaiOS mounts it
NAME_PATTERN = r'[a-z0-9][a-z0-9._-]{0,39}'

RECOVERY = Path(__file__).resolve().parent.parent / 'recovery'
UPDATE_BINARY = RECOVERY / 'update-binary'
BUSYBOX = RECOVERY / 'busybox'
CERTIFICATE = RECOVERY / 'testkey.x509.pem'
KEY = RECOVERY / 'testkey.pk8'

# Recovery's /default.prop is read directly: the legacy updater's live
# getprop() returns an empty device name on this phone.
UPDATER_SCRIPT = '''ui_print("KaiForward flasher: @NAME@");
ui_print("Partitions: @PARTITIONS@");
ui_print(concat("Recovery device: ", file_getprop("/default.prop", "ro.product.device")));
assert(file_getprop("/default.prop", "ro.product.device") == "@PRODUCT@");
unmount("/system");
mount("vfat", "EMMC", "/dev/block/mmcblk1p1", "/sdcard");
assert(package_extract_file("busybox", "/tmp/busybox"));
assert(package_extract_file("flash.sh", "/tmp/kfflash.sh"));
assert(package_extract_file("images", "/tmp/kfflash-images"));
set_perm(0, 0, 0755, "/tmp/busybox");
ui_print("Checking images and partitions...");
assert(run_program("/tmp/busybox", "sh", "/tmp/kfflash.sh", "check") == 0);
@WRITES@ui_print("Done; readback hashes verified. Log: kaiforward/@NAME@/flash.log");
ui_print("Select Reboot system now.");
'''

WRITE_STEP = '''ui_print("Writing @PARTITION@ (@DURATION@)...");
assert(run_program("/tmp/busybox", "sh", "/tmp/kfflash.sh", "write", "@PARTITION@") == 0);
'''

# Runs under the package's busybox, which must be named busybox: it treats any
# other name as the applet to run. Each line of /tmp/kfflash-images is
# "<partition> <bytes> <sha256>".
FLASH_SCRIPT = r'''#!/tmp/busybox sh
set -eu
BB=/tmp/busybox
IMAGES=/tmp/kfflash-images
CARD=/sdcard
DIRECTORY=$CARD/kaiforward/@NAME@
BY_NAME=/dev/block/bootdevice/by-name

fail() {
    echo "FAILED: $*"
    exit 1
}

"$BB" awk -v card="$CARD" '$1 == "/dev/block/mmcblk1p1" && $2 == card { found = 1 } END { exit !found }' /proc/mounts \
    || fail "microSD card is not mounted at $CARD"
[ -d "$DIRECTORY" ] || fail "image directory $DIRECTORY is missing"
exec >> "$DIRECTORY/flash.log" 2>&1
echo "== $* (uptime $("$BB" cut -d ' ' -f 1 /proc/uptime) s)"

# Refuse a partition mounted under any of its device names.
check_target() {
    target=$BY_NAME/$1
    [ -b "$target" ] || fail "$target is not a block device"
    [ "$("$BB" blockdev --getsize64 "$target")" = "$2" ] || fail "$1 partition is not $2 bytes"
    resolved=$("$BB" readlink -f "$target")
    for source in $("$BB" awk '$1 ~ /^\/dev\// { print $1 }' /proc/mounts); do
        [ "$("$BB" readlink -f "$source")" != "$resolved" ] || fail "$1 is mounted through $source"
    done
}

hash_of() {
    sum=$("$BB" sha256sum "$1")
    echo "${sum%% *}"
}

case $1 in
check)
    while read -r name size sha; do
        image=$DIRECTORY/$name.img
        [ -f "$image" ] || fail "$image is missing"
        [ "$("$BB" stat -c %s "$image")" = "$size" ] || fail "$name.img is not $size bytes"
        check_target "$name" "$size"
        [ "$(hash_of "$image")" = "$sha" ] || fail "$name.img does not match its hash"
        echo "$name.img verified: $sha"
    done < "$IMAGES"
    ;;
write)
    line=$("$BB" awk -v name="$2" '$1 == name' "$IMAGES")
    [ -n "$line" ] || fail "$2 is not in this package"
    set -- $line
    check_target "$1" "$2"
    echo "Writing $1"
    "$BB" dd if="$DIRECTORY/$1.img" of="$BY_NAME/$1" bs=1048576 conv=fsync
    "$BB" sync
    # Read back from the eMMC, not from the page cache the write filled.
    "$BB" blockdev --flushbufs "$BY_NAME/$1"
    echo 3 > /proc/sys/vm/drop_caches
    [ "$(hash_of "$BY_NAME/$1")" = "$3" ] || fail "$1 readback does not match; flash it again or restore the backup"
    echo "$1 readback verified: $3"
    ;;
*)
    fail "unknown step $1"
    ;;
esac
'''

# Rough write times on this phone's card and eMMC (about 10 MB/s).
WRITE_HINT = {'system': 'about 2 minutes', 'boot': 'a few seconds'}


class FlashError(RuntimeError):
    pass


def load_backup(backup, partitions):
    """Return the backup manifest after reverifying its images of the target partitions.

    The flasher only builds for a phone whose target partitions have a
    verified backup, so a restore package can always be built from it.
    """
    manifest = json.loads((backup / 'manifest.json').read_text())
    if manifest['device']['product'] != EXPECTED_PRODUCT:
        raise FlashError('Backup is not from a ' + EXPECTED_PRODUCT)
    for name in partitions:
        entry = manifest['images'].get(name)
        if not entry:
            raise FlashError('Backup {} has no {} image'.format(backup, name))
        image = backup / entry['file']
        if image.stat().st_size != entry['bytes'] or file_sha256(image) != entry['sha256']:
            raise FlashError('Backup image failed verification: ' + str(image))
    return manifest


def check_image(name, path, size):
    """Refuse images that do not fill the partition or are not the expected format."""
    if path.stat().st_size != size:
        raise FlashError('{} is {} bytes; the {} partition is {}'.format(path, path.stat().st_size, name, size))
    with path.open('rb') as image:
        if name == 'boot' and image.read(8) != b'ANDROID!':
            raise FlashError('{} is not an Android boot image'.format(path))
        if name == 'system':
            image.seek(1024 + 56)
            if image.read(2) != b'\x53\xef':
                raise FlashError('{} is not an ext4 image'.format(path))


def der(tag, *parts):
    body = b''.join(parts)
    if len(body) < 0x80:
        return bytes([tag, len(body)]) + body
    length = len(body).to_bytes((len(body).bit_length() + 7) // 8, 'big')
    return bytes([tag, 0x80 | len(length)]) + length + body


def der_elements(data):
    """Split concatenated DER elements into their complete encodings."""
    elements, offset = [], 0
    while offset < len(data):
        length, start = data[offset + 1], offset + 2
        if length & 0x80:
            count = length & 0x7f
            length, start = int.from_bytes(data[start:start + count], 'big'), start + count
        elements.append(data[offset:start + length])
        offset = start + length
    return elements


def der_body(element):
    return element[2 + (element[1] & 0x7f if element[1] & 0x80 else 0):]


SHA1 = der(0x30, bytes.fromhex('06052b0e03021a'), b'\x05\x00')
RSA = der(0x30, bytes.fromhex('06092a864886f70d010101'), b'\x05\x00')
PKCS7_DATA = bytes.fromhex('06092a864886f70d010701')
PKCS7_SIGNED_DATA = bytes.fromhex('06092a864886f70d010702')
SIGNED_BY = b'signed by SignApk\0'


def pkcs7_signature(content, certificate, key):
    """Detached PKCS#7 SHA1withRSA signature, encoded exactly as AOSP signapk does."""
    certificate_der = ssl.PEM_cert_to_DER_cert(certificate.read_text())
    tbs = der_elements(der_body(certificate_der))[0]
    fields = der_elements(der_body(tbs))
    if fields[0][0] == 0xa0:  # explicit version
        fields = fields[1:]
    serial, issuer = fields[0], fields[2]
    environment = dict(os.environ, OPENSSL_ENABLE_SHA1_SIGNATURES='1')  # stock recovery only checks SHA-1
    signature = subprocess.run(['openssl', 'dgst', '-sha1', '-sign', str(key), '-keyform', 'DER'],
                               input=content, capture_output=True, check=True, env=environment).stdout
    signer = der(0x30, der(0x02, b'\x01'), der(0x30, issuer, serial), SHA1, RSA, der(0x04, signature))
    signed_data = der(0x30, der(0x02, b'\x01'), der(0x31, SHA1), der(0x30, PKCS7_DATA),
                      der(0xa0, certificate_der), der(0x31, signer))
    return der(0x30, PKCS7_SIGNED_DATA, der(0xa0, signed_data))


def sign_package(unsigned, certificate, key):
    """Add the whole-file signature recovery verifies to a ZIP without a comment."""
    if unsigned[-22:-18] != b'PK\x05\x06' or unsigned[-2:] != b'\0\0':
        raise FlashError('Package must end with an empty ZIP comment')
    content = unsigned[:-2]  # everything before the comment length field
    signature = pkcs7_signature(content, certificate, key)
    size = len(SIGNED_BY) + len(signature) + 6
    comment = SIGNED_BY + signature + struct.pack('<3H', len(signature) + 6, 0xffff, size)
    if b'PK\x05\x06' in comment:
        raise FlashError('Signature contains a ZIP end marker; recovery would reject it')
    return content + struct.pack('<H', size) + comment


def verify_package(path, certificate):
    """Independently check the whole-file signature with openssl."""
    data = path.read_bytes()
    start, marker, size = struct.unpack('<3H', data[-6:])
    if marker != 0xffff or not 6 < start <= size or data[-size - 22:-size - 18] != b'PK\x05\x06':
        raise FlashError('Malformed whole-file signature footer')
    with tempfile.TemporaryDirectory() as temporary:
        signature, content = Path(temporary) / 'signature.der', Path(temporary) / 'content'
        signature.write_bytes(data[-start:-6])
        content.write_bytes(data[:-size - 2])
        environment = dict(os.environ, OPENSSL_ENABLE_SHA1_SIGNATURES='1')
        result = subprocess.run(['openssl', 'cms', '-verify', '-inform', 'DER', '-binary', '-in', str(signature),
                                 '-content', str(content), '-nointern', '-certfile', str(certificate),
                                 '-noverify', '-out', '/dev/null'], capture_output=True, text=True, env=environment)
    if result.returncode:
        raise FlashError('Package signature does not verify: ' + result.stderr.strip())


def fill(template, **values):
    for key, value in values.items():
        template = template.replace('@{}@'.format(key), value)
    if re.search(r'@[A-Z]+@', template):
        raise FlashError('Unfilled template placeholder')
    return template


def build(source, backup, name, partitions, output):
    """Create <output>/<name>.zip and <output>/<name>/<partition>.img, the card layout."""
    if not re.fullmatch(NAME_PATTERN, name):
        raise FlashError('Name must match ' + NAME_PATTERN)
    partitions = [partition for partition in PARTITIONS if partition in partitions]
    manifest = load_backup(backup, partitions)
    layout = manifest['layout']
    images = []
    for partition in partitions:
        path = source / (partition + '.img')
        check_image(partition, path, layout[partition]['bytes'])
        images.append((partition, path))
    if output.exists():
        raise FlashError('Refusing to replace ' + str(output))
    output.mkdir(parents=True)
    try:
        (output / name).mkdir()
        listing = ''
        for partition, path in images:
            copy = output / name / (partition + '.img')
            subprocess.run(['cp', '--reflink=auto', str(path), str(copy)], check=True)
            digest = file_sha256(copy)
            if digest != file_sha256(path):
                raise FlashError('Copy of {} does not match its source'.format(path))
            listing += '{} {} {}\n'.format(partition, copy.stat().st_size, digest)
        shell = fill(FLASH_SCRIPT, NAME=name)
        subprocess.run(['sh', '-n'], input=shell.encode(), check=True)
        writes = ''.join(fill(WRITE_STEP, PARTITION=partition, DURATION=WRITE_HINT[partition]) for partition in partitions)
        script = fill(UPDATER_SCRIPT, NAME=name, PARTITIONS=', '.join(partitions), PRODUCT=EXPECTED_PRODUCT, WRITES=writes)
        members = [
            ('META-INF/com/google/android/update-binary', UPDATE_BINARY.read_bytes()),
            ('META-INF/com/google/android/updater-script', script.encode()),
            ('busybox', BUSYBOX.read_bytes()),
            ('flash.sh', shell.encode()),
            ('images', listing.encode()),
        ]
        with tempfile.SpooledTemporaryFile() as buffer:
            with zipfile.ZipFile(buffer, 'w') as archive:
                for member, data in members:
                    archive.writestr(zipfile.ZipInfo(member, date_time=(2008, 1, 1, 0, 0, 0)), data, zipfile.ZIP_DEFLATED)
            buffer.seek(0)
            unsigned = buffer.read()
        package = output / (name + '.zip')
        package.write_bytes(sign_package(unsigned, CERTIFICATE, KEY))
        verify_package(package, CERTIFICATE)
        with zipfile.ZipFile(package) as archive:
            if archive.testzip() or [archive.read(member) for member, _ in members] != [data for _, data in members]:
                raise FlashError('Signed package contents changed')
        (output / 'SHA256SUMS').write_text(''.join('{}  {}\n'.format(file_sha256(path), path.relative_to(output))
                                                   for path in [package] + [output / name / (p + '.img') for p in partitions]))
    except BaseException:
        shutil.rmtree(output)
        raise
    return package


def push(adb, output, card=CARD, log=print):
    """Copy the package and its images to the phone's microSD card and check their hashes there."""
    sums = [line.split('  ', 1) for line in (output / 'SHA256SUMS').read_text().splitlines()]
    package = next(path for _, path in sums if path.endswith('.zip'))
    name = package[:-len('.zip')]
    remote = '{}/{}'.format(card, CARD_DIRECTORY)
    existing = adb.shell('for f in {r}/{p} {r}/{n}; do [ -e $f ] && echo $f; done; true'.format(
        r=remote, p=package, n=name)).split()
    if existing:
        raise FlashError('Already on the card: ' + ', '.join(existing))
    adb.shell('mkdir -p {r}/{n}'.format(r=remote, n=name))
    for digest, path in sums:
        log('Pushing ' + path)
        adb.run('push', str(output / path), '{}/{}'.format(remote, path))
        phone = adb.shell('busybox sha256sum {}/{}'.format(remote, path), timeout=600).split()[0]
        if phone != digest:
            raise FlashError('{} on the card does not match ({})'.format(path, phone))
    return '{}/{}'.format(CARD_DIRECTORY, package)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('source', type=Path, help='Directory containing boot.img and/or system.img')
    parser.add_argument('--backup', type=Path, required=True,
                        help='Verified backup of this phone (backups/<serial>-<timestamp>)')
    parser.add_argument('--name', required=True, help='Package name, such as gerda-native or restore-stock')
    parser.add_argument('--partitions', nargs='+', choices=PARTITIONS, default=list(PARTITIONS),
                        help='Partitions to flash (default: system boot)')
    parser.add_argument('--output', type=Path, help='Output directory (default: dist/<name>)')
    parser.add_argument('--push', action='store_true', help="Copy the result to the phone's microSD card over ADB")
    parser.add_argument('--serial', help='ADB serial for --push (default: the only connected device)')
    args = parser.parse_args(argv)
    os.umask(0o077)
    output = args.output or Path('dist') / args.name
    try:
        package = build(args.source, args.backup, args.name, args.partitions, output)
        print('Built and verified ' + str(package))
        if args.push:
            on_card = push(Adb(args.serial), output, log=lambda text: print(text, flush=True))
            print('On the card as ' + on_card)
    except (FlashError, AdbError, subprocess.CalledProcessError, OSError) as error:
        print('Flasher failed: {}'.format(error), file=sys.stderr)
        return 1
    print('In stock recovery choose "Apply update from SD card", then {}/{}.zip'.format(CARD_DIRECTORY, args.name))
    return 0


if __name__ == '__main__':
    sys.exit(main())
