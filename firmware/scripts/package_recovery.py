#!/usr/bin/env python3
"""Create a signed recovery ZIP that writes only boot and system; never install it."""
import argparse
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile
from recovery_device import DEVICE_CHECK

SCRIPT = '''ui_print("KaiForward experimental boot/system package");
''' + DEVICE_CHECK + '''
ui_print("Writing boot...");
assert(package_extract_file("boot.img", "/dev/block/bootdevice/by-name/boot"));
ui_print("Writing system...");
assert(package_extract_file("system.img", "/dev/block/bootdevice/by-name/system"));
ui_print("Boot/system written. No data wipe requested.");
'''


def package(gerda, images, destination):
    if destination.exists():
        raise ValueError('Refusing to replace an existing recovery package')
    for name, size in [('boot.img', 26165248), ('system.img', 838860800)]:
        if (images / name).stat().st_size != size:
            raise ValueError('Unexpected partition size: ' + name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='recovery-zip-') as temporary:
        unsigned = Path(temporary) / 'unsigned.zip'
        signed = Path(temporary) / 'signed.zip'
        with zipfile.ZipFile(unsigned, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for name in ['boot.img', 'system.img']:
                archive.write(images / name, name)
            archive.write(gerda / 'tools/stockerize/update-binary',
                          'META-INF/com/google/android/update-binary')
            archive.writestr('META-INF/com/google/android/updater-script', SCRIPT)
        subprocess.run([os.environ.get('GATEWAY_JAVA', 'java'), '-Xmx1024m', '-jar', str(gerda / 'tools/stockerize/signapk.jar'),
                        '-w', str(gerda / 'tools/keys/testkey.x509.pem'),
                        str(gerda / 'tools/keys/testkey.pk8'), str(unsigned), str(signed)], check=True)
        with zipfile.ZipFile(signed) as archive:
            if archive.testzip():
                raise ValueError('Recovery ZIP CRC check failed')
            if archive.read('META-INF/com/google/android/updater-script').decode() != SCRIPT:
                raise ValueError('Recovery script changed during signing')
            for name in ['boot.img', 'system.img']:
                with archive.open(name) as source, (images / name).open('rb') as original:
                    if hashlib.file_digest(source, 'sha256').digest() != hashlib.file_digest(original, 'sha256').digest():
                        raise ValueError('Packaged image hash mismatch: ' + name)
        signed.rename(destination)
    return hashlib.file_digest(destination.open('rb'), 'sha256').hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gerda', required=True, type=Path)
    parser.add_argument('--images', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    print(package(args.gerda.resolve(), args.images.resolve(), args.output.resolve()))
