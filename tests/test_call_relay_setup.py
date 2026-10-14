"""Call relay credential setup, with a mocked phone."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import call_relay


class CallRelaySetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'server'
        self.pushed = {}
        self.patches = []

    def run_setup(self, host='100.112.79.7', config='ssid=home\ntoken=phone-token\n', **options):
        with patch('scripts.call_relay.Adb') as factory, \
                patch('scripts.call_relay.settings_native.execute') as execute:
            adb = factory.return_value
            adb.serial = 'test-phone'
            adb.shell.side_effect = lambda command: '0\n' if command == 'id -u' else ''
            def run(action, source, target):
                if action == 'pull':
                    Path(target).write_text(config)
                else:
                    self.pushed[target] = Path(source).read_text()
            adb.run.side_effect = run
            execute.side_effect = lambda action, serial, path: self.patches.append(json.loads(path.read_text()))
            result = call_relay.setup('test-phone', host, output=self.output, **options)
            self.commands = [call.args[0] for call in adb.shell.call_args_list]
            return result

    def test_copies_token_creates_certificate_and_configures_phone(self):
        relay_url, created, _ = self.run_setup()
        self.assertEqual(relay_url, 'tls://100.112.79.7:8769')
        self.assertTrue(created)
        self.assertEqual((self.output / 'token').read_text(), 'phone-token\n')
        for name in ('token', 'relay.key', 'relay.crt'):
            self.assertEqual((self.output / name).stat().st_mode & 0o777, 0o600, name)
        self.assertTrue(call_relay.certificate_covers(self.output / 'relay.crt', '100.112.79.7'))
        self.assertEqual(self.pushed['/data/kaiforward/relay-ca.pem.new'], (self.output / 'relay.crt').read_text())
        self.assertIn('mv /data/kaiforward/relay-ca.pem.new /data/kaiforward/relay-ca.pem', self.commands[-1])
        self.assertEqual(self.patches, [{'relay_url': 'tls://100.112.79.7:8769'}])
        self.assertNotIn('phone-token', '\n'.join(self.commands))

    def test_keeps_a_matching_certificate_and_replaces_one_for_another_host(self):
        self.run_setup()
        original = (self.output / 'relay.crt').read_text()
        self.assertFalse(self.run_setup()[1])
        self.assertEqual((self.output / 'relay.crt').read_text(), original)
        self.assertTrue(self.run_setup(renew=True)[1])
        relay_url, created, _ = self.run_setup('fd7a:115c:a1e0::1', port=9000)
        self.assertTrue(created)
        self.assertEqual(relay_url, 'tls://[fd7a:115c:a1e0::1]:9000')
        self.assertTrue(call_relay.certificate_covers(self.output / 'relay.crt', 'fd7a:115c:a1e0::1'))

    def test_phone_without_token_is_not_changed(self):
        with self.assertRaisesRegex(ValueError, 'no token'):
            self.run_setup(config='ssid=home\ntoken=\n')
        self.assertEqual(self.pushed, {})
        self.assertEqual(self.patches, [])


if __name__ == '__main__':
    unittest.main()
