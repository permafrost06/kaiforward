"""SSH startup and guarded boot/transaction integration."""
import subprocess
import tempfile
from pathlib import Path
import unittest
from firmware.scripts.tailscale_boot import patch_boot
from scripts import ssh
from test_tailscale_install import boot_image, ramdisk_files


class SshTests(unittest.TestCase):
    def test_boot_service_preserves_tailscale_and_kernel(self):
        from firmware.scripts.tailscale_boot import SERVICE
        before = boot_image(SERVICE)
        after, manifest = patch_boot(before, ssh.SERVICE)
        self.assertTrue(manifest['changed'])
        startup = ramdisk_files(after)['init.kaiforward-native.rc'][1]
        self.assertIn(SERVICE, startup)
        self.assertIn(ssh.SERVICE, startup)
        self.assertEqual(patch_boot(after, ssh.SERVICE)[0], after)

    def test_conflicting_service_is_rejected(self):
        with self.assertRaises(ValueError):
            patch_boot(boot_image(b'service kf-ssh /wrong\n    class main\n'), ssh.SERVICE)

    def test_transaction_uses_only_ssh_paths(self):
        scripts = ssh.bundle_scripts('/data/kaiforward/ssh-installs/test', {
            'original_sha256': 'a' * 64, 'patched_sha256': 'b' * 64, 'bytes': 8388608})
        for name, body in scripts.items():
            self.assertNotIn('tailscale', body)
            self.assertIn('kf-ssh', body)
            self.assertIn('stop_pid "$runtime/dropbear.pid" "$runtime/dropbear"', body)
            self.assertNotIn('@', body)
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / name
                path.write_text(body)
                subprocess.run(['sh', '-n', str(path)], check=True)
        self.assertIn('"$bundle/dropbear" -V', scripts['apply.sh'])
        self.assertIn('mkdir -p "$runtime/.ssh"', scripts['apply.sh'])


class InstallerTests(unittest.TestCase):
    def setUp(self):
        import json
        from scripts.files import file_sha256
        from firmware.scripts.build_ssh import VERSION, SHA256
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        for name in ('dropbear', 'dropbearkey', 'DROPBEAR-LICENSE'):
            (self.runtime / name).write_text('test ' + name)
        config = ssh.PROJECT / 'firmware/native'
        self.metadata = {'version': VERSION, 'source_sha256': SHA256,
                         'files': {p.name: file_sha256(p) for p in self.runtime.iterdir()},
                         'configuration': {name: file_sha256(config / name)
                                           for name in ('ssh-account.h', 'ssh-options.h')}}
        (self.runtime / 'ssh-manifest.json').write_text(json.dumps(self.metadata))

    def test_install_requires_explicit_key_before_build_or_adb(self):
        from unittest.mock import patch
        with patch.object(ssh, 'prepare') as prepare, patch.object(ssh, 'phone') as phone:
            with self.assertRaisesRegex(ValueError, '--public-key'):
                ssh.install()
            prepare.assert_not_called()
            phone.assert_not_called()

    def test_cli_requires_explicit_key(self):
        result = subprocess.run(['python3', '-m', 'scripts.ssh', 'install'],
                                cwd=ssh.PROJECT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--public-key', result.stderr)

    def test_explicit_runtime_rejects_corruption_without_building(self):
        from unittest.mock import patch
        (self.runtime / 'dropbear').write_text('corrupt')
        with patch('firmware.scripts.build_ssh.build') as builder:
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                ssh.prepare(runtime=self.runtime)
            builder.assert_not_called()

    def test_first_prepare_builds_and_later_prepare_reuses_cache(self):
        import shutil
        from unittest.mock import patch
        cache = self.root / 'cache'
        def build(build_cache, output, zig):
            shutil.copytree(self.runtime, output)
        with patch('firmware.scripts.build_ssh.build', side_effect=build) as builder:
            self.assertEqual(ssh.prepare(cache), cache / 'runtime')
            self.assertEqual(ssh.prepare(cache), cache / 'runtime')
            self.assertEqual(builder.call_count, 1)

    def test_identical_install_preserves_boot_payload_and_disabled_state(self):
        import hashlib
        import shutil
        from unittest.mock import Mock, patch
        from scripts.files import file_sha256
        key = self.root / 'key.pub'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.root / 'key')], check=True)
        original = boot_image(ssh.SERVICE)
        digest = hashlib.sha256(original).hexdigest()
        adb = Mock(serial='test-phone')
        hashes = {}
        for name, (target, _) in ssh.FILES.items():
            if name == 'authorized_keys':
                hashes[target] = file_sha256(key)
            elif name == 'startup.sh':
                hashes[target] = file_sha256(ssh.PROJECT / 'firmware/native/ssh.sh')
            else:
                hashes[target] = file_sha256(self.runtime / name)
        def shell(command):
            if command.startswith('busybox sha256sum '):
                return digest + '  boot-before.img\n'
            if command.startswith('for target in '):
                return ''.join(value + '  ' + target + '\n' for target, value in hashes.items())
            if '/disabled' in command:
                return 'disabled\n'
            return ''
        def run(action, remote, local):
            self.assertEqual(action, 'pull')
            Path(local).write_bytes(original)
        adb.shell.side_effect = shell
        adb.run.side_effect = run
        # Keep real configuration/runtime validation; redirect only backups.
        project = self.root / 'project'
        (project / 'firmware').mkdir(parents=True)
        shutil.copytree(ssh.PROJECT / 'firmware/native', project / 'firmware/native')
        with patch.object(ssh, 'PROJECT', project), patch.object(ssh, 'phone', return_value=adb), \
                patch.object(ssh, 'manage') as manage:
            result = ssh.install(runtime=self.runtime, public_key=key)
        self.assertFalse(result['changed'])
        self.assertEqual(adb.run.call_count, 1)
        manage.assert_not_called()
        self.assertFalse(any('of=' + ssh.BOOT in call.args[0] for call in adb.shell.call_args_list))
