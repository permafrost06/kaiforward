"""Verify guarded boot edits and Tailscale install/rollback against disposable files."""
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from firmware.scripts.cpio import cpio_files
from firmware.scripts.image_utils import BOOT_SIZE
from firmware.scripts.tailscale_boot import patch_boot, SERVICE
from scripts import tailscale


def digest(content):
    return hashlib.sha256(content).hexdigest()


def archive_files(entries):
    parts = []
    for index, (name, mode, content) in enumerate([*entries, ('TRAILER!!!', 0, b'')]):
        filename = ('./' + name if name != 'TRAILER!!!' else name).encode() + b'\0'
        fields = [index + 1, mode, 0, 0, 1, 1234, len(content), 0, 0, 0, 0, len(filename), 0]
        prefix = b'070701' + b''.join(f'{value:08x}'.encode() for value in fields) + filename
        entry = prefix + b'\0' * (-len(prefix) % 4) + content
        parts.append(entry + b'\0' * (-len(entry) % 4))
    result = b''.join(parts)
    return result + b'\0' * (-len(result) % 512)


def boot_image(startup=b'service custom /system/bin/custom\n    class main\n', signed=False):
    kernel = b'original-kernel-and-device-trees' * 100
    archive = archive_files([
        ('init.kaiforward-native.rc', 0o100640, startup),
        ('init.b2g.rc', 0o100644, b'import /init.kaiforward-native.rc\n'),
        ('other.conf', 0o100400, b'device-specific content\0'),
        ('etc', 0o120777, b'/system/etc'),
    ])
    compressed = gzip.compress(archive, mtime=0)
    header = bytearray(2048)
    header[:8] = b'ANDROID!'
    struct.pack_into('<10I', header, 8, len(kernel), 0x80008000, len(compressed),
                     0x81000000, 0, 0x80f00000, 0x80000100, 2048, 0, 0)
    align = lambda size: (size + 2047) // 2048 * 2048
    result = bytes(header) + kernel + b'\0' * (align(len(kernel)) - len(kernel))
    result += compressed + b'\0' * (align(len(compressed)) - len(compressed))
    result += b'\0' * (BOOT_SIZE - len(result))
    if signed:
        result = result[:-4] + b'SIGN'
    return result


def ramdisk_files(raw):
    kernel, ramdisk = struct.unpack_from('<I', raw, 8)[0], struct.unpack_from('<I', raw, 16)[0]
    offset = 2048 + (kernel + 2047) // 2048 * 2048
    return cpio_files(gzip.decompress(raw[offset:offset + ramdisk]))


class BootPatchTests(unittest.TestCase):
    def test_patch_preserves_kernel_symlink_modes_and_custom_startup(self):
        original = boot_image()
        modified, manifest = patch_boot(original)
        before, after = ramdisk_files(original), ramdisk_files(modified)
        self.assertEqual(len(modified), BOOT_SIZE)
        kernel_size = struct.unpack_from('<I', original, 8)[0]
        self.assertEqual(original[2048:2048 + kernel_size], modified[2048:2048 + kernel_size])
        self.assertEqual(after['init.kaiforward-native.rc'][0], before['init.kaiforward-native.rc'][0])
        self.assertTrue(after['init.kaiforward-native.rc'][1].startswith(before['init.kaiforward-native.rc'][1]))
        for name in ('init.b2g.rc', 'etc', 'other.conf'):
            self.assertEqual(before[name], after[name])
        self.assertTrue(manifest['changed'])

    def test_second_install_keeps_boot_image_byte_identical(self):
        modified, _ = patch_boot(boot_image())
        again, manifest = patch_boot(modified)
        self.assertEqual(again, modified)
        self.assertFalse(manifest['changed'])

    def test_conflicting_existing_service_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'differs'):
            patch_boot(boot_image(SERVICE.replace(b'root inet', b'system inet')))

    def test_trailing_signature_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'signature'):
            patch_boot(boot_image(signed=True))

    def test_unknown_layout_is_rejected(self):
        original = bytearray(boot_image())
        struct.pack_into('<I', original, 40, 1024)
        with self.assertRaisesRegex(ValueError, 'layout'):
            patch_boot(bytes(original))

    def test_nonzero_boot_id_is_verified_and_recomputed(self):
        raw = bytearray(boot_image())
        kernel_size = struct.unpack_from('<I', raw, 8)[0]
        ramdisk_size = struct.unpack_from('<I', raw, 16)[0]
        offset = 2048 + (kernel_size + 2047) // 2048 * 2048
        def image_id(image):
            h = hashlib.sha1()
            size = struct.unpack_from('<I', image, 16)[0]
            for content in (image[2048:2048 + kernel_size], image[offset:offset + size], b''):
                h.update(content)
                h.update(struct.pack('<I', len(content)))
            return h.digest() + b'\0' * 12
        raw[576:608] = image_id(raw)
        modified, _ = patch_boot(bytes(raw))
        self.assertEqual(modified[576:608], image_id(modified))
        raw[576] ^= 1
        with self.assertRaisesRegex(ValueError, 'boot ID'):
            patch_boot(bytes(raw))


class DownloadValidationTests(unittest.TestCase):
    def test_wrong_archive_rejected_before_adb(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'wrong.tgz'
            archive.write_bytes(b'not a verified archive')
            with patch.object(tailscale, 'Adb') as adb:
                with self.assertRaisesRegex(ValueError, 'does not match'):
                    tailscale.install(cache=root / 'cache', archive=archive)
            adb.assert_not_called()

    def test_verified_archive_still_requires_arm_and_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / 'wrong-architecture.tgz'
            header = bytearray(20)
            header[:7] = b'\x7fELF\x01\x01\x01'
            header[18] = 62  # x86-64
            with tarfile.open(archive, 'w:gz') as bundle:
                member = tarfile.TarInfo('tailscale_test_arm/tailscale')
                member.size = len(header)
                bundle.addfile(member, io.BytesIO(header))
            pinned = {'version': 'test', 'sha256': digest(archive.read_bytes()), 'url': 'unused'}
            with patch.object(tailscale, 'inputs', return_value=pinned):
                with self.assertRaisesRegex(ValueError, 'not 32-bit'):
                    tailscale.prepare(root / 'cache', archive=archive)

    def test_rollback_path_injection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Invalid installation'):
            tailscale.manage('rollback', adb=object(), install_id='../../config; reboot')


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / 'data/kaiforward/tailscale-installs/test'
        self.bundle.mkdir(parents=True)
        self.runtime = self.root / 'data/kaiforward/tailscale'
        self.runtime.mkdir()
        self.startup = self.root / 'system/kaiforward/tailscale.sh'
        self.startup.parent.mkdir(parents=True)
        self.boot = self.root / 'boot.img'
        self.boot.write_bytes(b'old-boot')
        (self.bundle / 'boot-before.img').write_bytes(b'old-boot')
        (self.bundle / 'boot-installed.img').write_bytes(b'new-boot')
        self.original = {}
        self.plan = {}
        for name, (target, mode) in tailscale.FILES.items():
            actual = self.root / target.lstrip('/')
            actual.parent.mkdir(parents=True, exist_ok=True)
            content = ('#!/bin/sh\necho 1.104.1\n' if name == 'tailscale' else
                       '#!/bin/sh\nexit 0\n' if name == 'startup.sh' else 'new ' + name)
            (self.bundle / name).write_text(content)
            actual.write_text('old ' + name)
            actual.chmod(0o600)
            self.original[name] = actual.read_bytes()
            self.plan[name] = (actual, mode)
        # Private state must survive both installation and rollback.
        (self.runtime / 'state').mkdir()
        (self.runtime / 'state/tailscaled.state').write_text('private login state')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.root / 'mount-mode').write_text('ro')
        helper = '''#!/usr/bin/env python3
import os, pathlib, subprocess, sys
root=pathlib.Path(os.environ['FAKE_PHONE'])
tool=pathlib.Path(sys.argv[0]).name
args=sys.argv[1:]
if tool=='busybox':
    tool,args=args[0],args[1:]
if tool=='getprop': sys.exit(0)
if tool in ('stop','start'): sys.exit(0)
if tool=='blockdev':
    print((root/'boot.img').stat().st_size);sys.exit(0)
if tool=='mount':
    (root/'mount-mode').write_text('ro' if 'remount,ro' in args else 'rw');sys.exit(0)
if tool=='cp' and (root/'fail-copy').exists() and args[-1].endswith('tailscaled.new'):
    (root/'fail-copy').unlink();sys.exit(1)
if tool=='cp' and (root/'fail-restore').exists() and args[-1].endswith('tailscaled.restore'):
    (root/'fail-restore').unlink();sys.exit(1)
if tool=='dd' and (root/'fail-boot').exists():
    (root/'fail-boot').unlink()
    (root/'boot.img').write_bytes(b'partial!');sys.exit(1)
os.execvp(tool,[tool,*args])
'''
        for name in ('busybox', 'getprop', 'stop', 'start', 'blockdev'):
            path = self.bin / name
            path.write_text(helper)
            path.chmod(0o755)
        manifest = {'original_sha256': digest(b'old-boot'), 'patched_sha256': digest(b'new-boot'), 'bytes': 8}
        for name, content in tailscale.bundle_scripts(str(self.bundle), manifest).items():
            content = content.replace('PATH=/system/bin:/system/xbin', 'PATH=' + str(self.bin) + ':/usr/bin:/bin')
            content = content.replace('/system/bin/sh', '/bin/sh')
            content = content.replace('/dev/block/bootdevice/by-name/boot', str(self.boot))
            content = content.replace('/data/kaiforward', str(self.root / 'data/kaiforward'))
            content = content.replace('/system/kaiforward', str(self.root / 'system/kaiforward'))
            content = content.replace("options=$(busybox awk '$2 == \"/system\" {print $4}' /proc/mounts)",
                                      'options=$(cat ' + str(self.root / 'mount-mode') + ')')
            # The already-expanded bundle path must not be translated twice.
            content = content.replace(str(self.root) + str(self.root), str(self.root))
            (self.bundle / name).write_text(content)
        (self.bundle / 'files').write_text(''.join(f'{name} {target} {mode}\n' for name, (target, mode) in self.plan.items()))
        self.checksums()

    def checksums(self):
        names = [*self.plan, 'files', 'boot-before.img', 'boot-installed.img', 'apply.sh', 'rollback.sh']
        (self.bundle / 'SHA256SUMS').write_text(''.join(digest((self.bundle / name).read_bytes()) + '  ' + name + '\n' for name in names))

    def run_script(self, name):
        return subprocess.run(['/bin/sh', str(self.bundle / name)], capture_output=True, text=True,
                              timeout=30, env=dict(os.environ, FAKE_PHONE=str(self.root)))

    def assert_original(self):
        self.assertEqual(self.boot.read_bytes(), b'old-boot')
        for name, (target, _) in self.plan.items():
            self.assertEqual(target.read_bytes(), self.original[name], name)
        self.assertEqual((self.runtime / 'state/tailscaled.state').read_text(), 'private login state')
        self.assertEqual((self.root / 'mount-mode').read_text(), 'ro')

    def test_install_and_rollback_preserve_login_and_previous_modes(self):
        result = self.run_script('apply.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.boot.read_bytes(), b'new-boot')
        for name, (target, mode) in self.plan.items():
            self.assertEqual(target.read_bytes(), (self.bundle / name).read_bytes())
            self.assertEqual(target.stat().st_mode & 0o777, int(mode, 8))
        result = self.run_script('rollback.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_original()
        self.assertTrue(all(target.stat().st_mode & 0o777 == 0o600 for target, _ in self.plan.values()))

    def test_fresh_install_rollback_removes_payload_but_retains_login(self):
        for target, _ in self.plan.values():
            target.unlink()
        result = self.run_script('apply.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.run_script('rollback.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(all(not target.exists() for target, _ in self.plan.values()))
        self.assertEqual(self.boot.read_bytes(), b'old-boot')
        self.assertEqual((self.runtime / 'state/tailscaled.state').read_text(), 'private login state')

    def test_damaged_transfer_is_rejected_before_changing_files(self):
        (self.bundle / 'tailscaled').write_text('damaged transfer')
        result = self.run_script('apply.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assert_original()

    def test_partial_copy_failure_recovers_previous_runtime(self):
        (self.root / 'fail-copy').touch()
        result = self.run_script('apply.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assert_original()

    def test_partial_boot_write_is_restored_and_verified(self):
        (self.root / 'fail-boot').touch()
        result = self.run_script('apply.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assert_original()

    def test_newer_boot_blocks_rollback(self):
        result = self.run_script('apply.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.boot.write_bytes(b'newer!!!')
        result = self.run_script('rollback.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.boot.read_bytes(), b'newer!!!')
        self.assertEqual(self.plan['tailscaled'][0].read_bytes(), (self.bundle / 'tailscaled').read_bytes())

    def test_failed_rollback_recovers_the_installed_runtime(self):
        result = self.run_script('apply.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        (self.root / 'fail-restore').touch()
        result = self.run_script('rollback.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.boot.read_bytes(), b'new-boot')
        for name, (target, _) in self.plan.items():
            self.assertEqual(target.read_bytes(), (self.bundle / name).read_bytes())
        self.assertEqual((self.runtime / 'state/tailscaled.state').read_text(), 'private login state')


if __name__ == '__main__':
    unittest.main()
