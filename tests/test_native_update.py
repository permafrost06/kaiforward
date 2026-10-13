"""Exercise the on-device update transaction in a disposable fake Android system."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import update_native as update
from scripts.adb import AdbError
from scripts.files import file_sha256


class RuntimeValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        source = self.root / 'firmware/native'
        source.mkdir(parents=True)
        header = bytearray(20)
        header[:7] = b'\x7fELF\x01\x01\x01'
        header[18] = 40
        (self.runtime / 'kaiforward-native').write_bytes(header)
        hashes = {}
        for name in [*update.PAYLOAD, 'init.native.rc']:
            if name == 'kaiforward-native':
                continue
            (source / name).write_text('source ' + name)
            shutil.copyfile(source / name, self.runtime / name)
            hashes['firmware/native/' + name] = file_sha256(source / name)
        self.manifest = {'sources': hashes, 'binary_sha256': file_sha256(self.runtime / 'kaiforward-native')}
        self.write_manifest()
        self.project = patch.object(update, 'PROJECT', self.root)
        self.project.start()
        self.addCleanup(self.project.stop)

    def write_manifest(self):
        (self.runtime / 'native-manifest.json').write_text(json.dumps(self.manifest))

    def test_validated_files_exclude_startup_and_private_settings(self):
        files, digest = update.validate_runtime(self.runtime)
        self.assertEqual(set(files), set(update.PAYLOAD))
        self.assertEqual(digest, file_sha256(self.runtime / 'init.native.rc'))
        self.assertNotIn('config', files)
        self.assertNotIn('ca.pem', files)

    def test_source_change_rejects_before_connecting_to_adb(self):
        (self.root / 'firmware/native/wifi.sh').write_text('changed source')
        with patch.object(update, 'Adb') as adb:
            with self.assertRaisesRegex(ValueError, 'Sources changed'):
                update.install(self.runtime)
        adb.assert_not_called()

    def test_modified_runtime_script_is_rejected(self):
        (self.runtime / 'wifi.sh').write_text('damaged copy')
        with self.assertRaisesRegex(ValueError, 'Runtime file'):
            update.validate_runtime(self.runtime)

    def test_host_binary_is_rejected_even_with_matching_hash(self):
        binary = self.runtime / 'kaiforward-native'
        data = bytearray(binary.read_bytes())
        data[18] = 62  # x86-64
        binary.write_bytes(data)
        self.manifest['binary_sha256'] = file_sha256(binary)
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, 'ARM binary'):
            update.validate_runtime(self.runtime)

    def test_failed_push_does_not_execute_update(self):
        with patch.object(update, 'Adb') as factory, patch.object(update, 'check_phone', return_value={}):
            adb = factory.return_value
            adb.run.side_effect = AdbError('transfer failed')
            with self.assertRaises(AdbError):
                update.install(self.runtime, serial='test-device')
        self.assertFalse(any('/apply.sh' in call.args[0] for call in adb.shell.call_args_list))

    def test_rollback_rejects_path_injection_before_adb(self):
        with patch.object(update, 'Adb') as adb:
            with self.assertRaises(ValueError):
                update.rollback('../../config; reboot')
        adb.assert_not_called()


@unittest.skipUnless(shutil.which('sha256sum'), 'host checksum tool required')
class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.system = self.root / 'system'
        self.home = self.root / 'data/kaiforward'
        self.update_id = '20261014-010000-01234567'
        self.bundle = self.home / 'updates' / self.update_id
        self.bundle.mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.state = self.root / 'services'
        self.state.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        for name in update.SERVICES:
            (self.state / name).write_text('running')
        (self.root / 'mount-mode').write_text('ro')
        self.original = {}
        self.new = {}
        self.names = [*update.PAYLOAD, 'kaiforward-relay']
        self.plan = dict(update.PAYLOAD, **{'kaiforward-relay': ('/system/bin/kaiforward-relay', '755')})
        for name, (target, mode) in self.plan.items():
            target = self.root / target.lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('old ' + name)
            target.chmod(int(mode, 8))
            content = ('#!/bin/sh\necho 192.0.2.0/24\n' if name == 'kaiforward-native' else 'new ' + name)
            (self.bundle / name).write_text(content)
            self.original[name] = target.read_bytes()
            self.new[name] = content.encode()
        self.init = self.root / 'init.kaiforward-native.rc'
        self.init.write_text('unchanged init')
        self.config = self.home / 'config'
        self.config.write_text('private settings')
        self.db = self.home / 'native.sqlite3'
        self.db.write_bytes(b'sms database')
        self.write_scripts()
        self.write_tools()
        self.refresh_checksums()

    def write_scripts(self):
        for name, script in update.bundle_scripts(self.update_id, self.names, file_sha256(self.init)).items():
            script = script.replace('PATH=/system/bin:/system/xbin', 'PATH=' + str(self.bin) + ':/usr/bin:/bin')
            script = script.replace('/data/kaiforward', str(self.home))
            script = script.replace('/init.kaiforward-native.rc', str(self.init))
            script = script.replace('/system', str(self.system))
            script = script.replace('/proc/[0-9]*/exe', str(self.proc) + '/[0-9]*/exe')
            (self.bundle / name).write_text(script)
        (self.bundle / 'files').write_text(''.join(
            f'{name} {self.root / target.lstrip("/")} {mode}\n' for name, (target, mode) in self.plan.items()))

    def refresh_checksums(self):
        names = [*self.names, 'files', 'apply.sh', 'rollback.sh']
        (self.bundle / 'SHA256SUMS').write_text(''.join(file_sha256(self.bundle / name) + '  ' + name + '\n' for name in names))

    def write_tools(self):
        # Only init/property/mount plumbing is mocked. cp/mv/checksums/modes and
        # the transaction's error and signal traps execute as real processes.
        helper = '''import os, pathlib, signal, subprocess, sys
root = pathlib.Path(os.environ['FAKE_PHONE'])
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
def consume(name):
    marker = root / name
    if marker.exists():
        marker.unlink()
        return True
    return False
if tool == 'getprop':
    print((root / 'services' / args[0].removeprefix('init.svc.')).read_text())
elif tool == 'setprop':
    action, name = args
    with (root / 'events').open('a') as log:
        log.write(action + ' ' + name + '\\n')
    state = 'running' if action == 'ctl.start' else 'stopped'
    if action == 'ctl.start' and name == 'kaiforward-ui' and consume('crash-on-start'):
        (root / 'crash-pending').touch()
    (root / 'services' / name).write_text(state)
    if name == 'kaiforward-ui' and action == 'ctl.start':
        process = root / 'proc/123'
        process.mkdir(exist_ok=True)
        if not consume('retain-old-executable'):
            (process / 'exe').write_bytes((root / 'system/bin/kaiforward-native').read_bytes())
elif tool == 'sleep':
    if consume('crash-pending'):
        (root / 'services/kaiforward-ui').write_text('stopped')
elif tool == 'id':
    print('0')
elif tool == 'busybox':
    command, *rest = args
    if command == 'mount':
        mode = rest[1].split(',')[1]
        (root / 'mount-mode').write_text(mode)
        with (root / 'mount-events').open('a') as log:
            log.write(mode + '\\n')
    elif command == 'awk' and rest[-1] == '/proc/mounts':
        print((root / 'mount-mode').read_text())
    elif command in ('chown', 'sync'):
        pass
    elif command == 'grep' and '-qx' in rest:
        # Reproduce the target BusyBox's broken exact-line matching.
        sys.exit(1)
    elif command == 'readlink' and rest[-1].endswith('/proc/123/exe'):
        print(root / 'system/bin/kaiforward-native')
    elif command == 'cp' and rest[-1].endswith('wifi.sh.kf-update') and consume('fail-copy'):
        sys.exit(1)
    else:
        result = subprocess.run([command, *rest])
        if result.returncode:
            sys.exit(result.returncode)
        if command == 'mv' and rest[-1].endswith('/wifi.sh') and consume('interrupt'):
            os.kill(os.getppid(), signal.SIGTERM)
else:
    sys.exit('Unexpected tool: ' + tool)
'''
        for tool in ['busybox', 'getprop', 'setprop', 'sleep', 'id']:
            path = self.bin / tool
            path.write_text('#!' + sys.executable + '\n' + helper)
            path.chmod(0o755)

    def run_script(self, name='apply.sh'):
        env = dict(os.environ, FAKE_PHONE=str(self.root))
        return subprocess.run(['/bin/sh', str(self.bundle / name)], env=env, capture_output=True, text=True, timeout=30)

    def assert_files(self, expected):
        for name, (target, mode) in self.plan.items():
            path = self.root / target.lstrip('/')
            self.assertEqual(path.read_bytes(), expected[name], name)
            self.assertEqual(path.stat().st_mode & 0o777, int(mode, 8))
        self.assertEqual(self.config.read_text(), 'private settings')
        self.assertEqual(self.db.read_bytes(), b'sms database')
        self.assertFalse((self.home / 'update.lock').exists())
        self.assertEqual((self.root / 'mount-mode').read_text(), 'ro')

    def assert_services_running(self):
        self.assertTrue(all((self.state / name).read_text() == 'running' for name in update.SERVICES))

    def test_update_and_explicit_rollback_preserve_data_and_modes(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_files(self.new)
        self.assert_services_running()
        result = self.run_script('rollback.sh')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_files(self.original)
        self.assert_services_running()

    def test_corrupt_transfer_changes_no_runtime_or_service(self):
        (self.bundle / 'wifi.sh').write_text('corrupt transfer')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assert_files(self.original)
        self.assertFalse((self.root / 'events').exists())
        self.assertFalse((self.root / 'mount-events').exists())

    def test_init_mismatch_requires_images_without_stopping_services(self):
        self.init.write_text('changed init')
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Startup definitions differ', result.stderr)
        self.assert_files(self.original)
        self.assertFalse((self.root / 'events').exists())

    def test_copy_failure_after_binary_install_restores_entire_runtime(self):
        (self.root / 'fail-copy').touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Previous runtime restored', result.stderr)
        self.assert_files(self.original)
        self.assert_services_running()

    def test_signal_after_partial_install_restores_entire_runtime(self):
        (self.root / 'interrupt').touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Previous runtime restored', result.stderr)
        self.assert_files(self.original)
        self.assert_services_running()

    def test_startup_crash_rolls_back_to_previous_runtime(self):
        (self.root / 'crash-on-start').touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('startup check', result.stderr)
        self.assert_files(self.original)
        self.assert_services_running()

    def test_running_property_with_old_executable_triggers_rollback(self):
        process = self.proc / '123'
        process.mkdir()
        (process / 'exe').write_bytes(self.original['kaiforward-native'])
        (self.root / 'retain-old-executable').touch()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Running process still uses another binary', result.stderr)
        self.assert_files(self.original)
        self.assert_services_running()

    def test_failed_rollback_restores_the_runtime_in_use_before_rollback(self):
        self.assertEqual(self.run_script().returncode, 0)
        (self.root / 'fail-copy').touch()
        result = self.run_script('rollback.sh')
        self.assertNotEqual(result.returncode, 0)
        self.assert_files(self.new)
        self.assert_services_running()

    def test_stopped_service_stays_stopped(self):
        (self.state / 'kf-upload').write_text('stopped')
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.state / 'kf-upload').read_text(), 'stopped')
        self.assertNotIn('kf-upload', (self.root / 'events').read_text())

    def test_existing_lock_is_retained_and_blocks_updates(self):
        lock = self.home / 'update.lock'
        lock.mkdir()
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(lock.exists())
        self.assertFalse((self.root / 'events').exists())


if __name__ == '__main__':
    unittest.main()
