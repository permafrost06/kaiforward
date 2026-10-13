"""Provisioning must stop when the phone reports failure through legacy ADB."""
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.adb import AdbError
from scripts.configure_native import install


class ProvisioningTests(unittest.TestCase):
    def test_missing_native_runtime_stops_before_push(self):
        def response(command, **kwargs):
            output = '0\r\n<rc=0>\r\n' if 'id -u' in command[-1] else '<rc=1>\r\n'
            return subprocess.CompletedProcess(command, 0, output, '')

        with patch('scripts.adb.subprocess.run', side_effect=response) as run:
            with self.assertRaises(AdbError):
                install(Path('/unused'), 'test-device')
        self.assertEqual(run.call_count, 2)
        self.assertTrue(all(call.args[0][:3] == ['adb', '-s', 'test-device'] for call in run.call_args_list))

    def test_failed_permissions_do_not_rename_or_request_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config').write_text('test settings')

            def response(command, **kwargs):
                if command[3] == 'push':
                    return subprocess.CompletedProcess(command, 0, '', '')
                shell = command[-1]
                if shell.startswith('chmod 600 '):
                    return subprocess.CompletedProcess(command, 0, '<rc=1>\n', '')
                output = '0\n' if shell.startswith('id -u') else ''
                return subprocess.CompletedProcess(command, 0, output + '<rc=0>\n', '')

            with patch('scripts.adb.subprocess.run', side_effect=response) as run:
                with self.assertRaises(AdbError):
                    install(root, 'test-device')
            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(sum(command[3] == 'push' for command in commands), 1)
            self.assertIn(' && mv ', commands[-1][-1])
            self.assertFalse(any('touch /data/kaiforward/reload' in command[-1] for command in commands))
