"""Settings commands and the phone-side HTTP API, without hardware."""
from contextlib import closing
import fcntl
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import test_native as fixtures
from scripts import settings_native

SOURCES = Path(__file__).resolve().parents[1] / 'firmware/native'


class NativeSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / 'native'
        subprocess.run(['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-misleading-indentation', '-I' + str(SOURCES),
                        *map(str, sorted(SOURCES.glob('*.c'))), '-lsqlite3',
                        '-o', str(cls.binary)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def command(self, action, data=None, check=True):
        return subprocess.run([str(self.binary), '--home', str(self.home), action],
                              input=data, capture_output=True, text=True, check=check)

    def update(self, values):
        return json.loads(self.command('--settings-set', json.dumps(values)).stdout)

    def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.key = self.command('--settings-key').stdout.strip()
        self.process = subprocess.Popen([str(self.binary), '--home', str(self.home),
                                         '--socket-dir', str(self.home),
                                         '--settings-bind', '127.0.0.1',
                                         '--settings-port', str(self.port)],
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        process = self.process
        def stop():
            if process.poll() is None:
                process.terminate()
            process.communicate(timeout=5)
        self.addCleanup(stop)
        def listening():
            try:
                with socket.create_connection(('127.0.0.1', self.port), timeout=.1):
                    return True
            except OSError:
                return False
        fixtures.eventually(listening)

    def request(self, method='GET', body=None, headers=None, path='/v1/settings', auth=True):
        fields = {'Authorization': 'Bearer ' + self.key} if auth else {}
        if body is not None:
            fields['Content-Type'] = 'application/json'
        fields.update(headers or {})
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=fields)
            response = conn.getresponse()
            data = response.read().decode()
            self.assertEqual(response.getheader('Cache-Control'), 'no-store')
            self.assertEqual(response.getheader('Connection'), 'close')
            return response.status, json.loads(data)
        finally:
            conn.close()

    def raw(self, data):
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as conn:
            conn.sendall(data)
            response = http.client.HTTPResponse(conn)
            response.begin()
            response.read()
            return response.status

    def test_cli_partial_updates_private_storage_and_defaults(self):
        defaults = json.loads(self.command('--settings').stdout)
        self.assertEqual(defaults, dict(ssid='', url='',
                                       password_set=False, token_set=False))
        values = dict(ssid='ঢাকা "home"', password='private=password',
                      url='https://example.org/v1/messages', token='private-token')
        result = self.update(values)
        self.assertTrue(result['password_set'])
        self.assertTrue(result['token_set'])
        self.assertNotIn('password', result)
        self.assertNotIn('token', result)
        saved = (self.home / 'config').read_text()
        self.assertIn('password=private=password\n', saved)
        self.assertEqual((self.home / 'config').stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.home / 'reload').exists())
        self.assertTrue((self.home / 'sync').exists())
        result = self.update({'url': 'https://example.org/v2/messages'})
        self.assertEqual(result['ssid'], values['ssid'])
        self.assertEqual(result['url'], 'https://example.org/v2/messages')
        self.assertNotIn('device_id', result)
        self.assertIn('token=private-token\n', (self.home / 'config').read_text())
        wifi = self.command('--wifi-config').stdout
        self.assertIn('ssid="ঢাকা \\"home\\""', wifi)
        self.assertIn('psk="private=password"', wifi)
        result = self.update(dict(password='', url='', token=''))
        self.assertFalse(result['password_set'])
        self.assertFalse(result['token_set'])
        self.assertIn('key_mgmt=NONE', self.command('--wifi-config').stdout)
        self.assertEqual(json.loads(self.command('--settings').stdout), result)

    def test_invalid_patches_do_not_change_settings_or_request_reload(self):
        self.update(dict(ssid='home', password='password12', url='http://host/original'))
        saved = (self.home / 'config').read_bytes()
        (self.home / 'reload').unlink()
        (self.home / 'sync').unlink()
        invalid = ['{}', '[]', 'null', '{', '{"ssid":"one","ssid":"two"}',
                   '{"ssid\\u0000extra":"one"}', '{"ssid":"bad\\u0000name"}',
                   '{"ssid":"ok"} trailing']
        invalid += [json.dumps(p) for p in [dict(unknown='field'), dict(password=None),
                    dict(password=123), dict(ssid='x'*33), dict(ssid='ঢ'*11),
                    dict(password='short'), dict(password='z'*64), dict(device_id='removed'),
                    dict(token='x'*257), dict(url='x'*1025),
                    dict(token='header\nInjected'), dict(url='file:///etc/passwd'),
                    dict(url='https://'), dict(url='http://?query'),
                    dict(url='https://user:pass@example.org'),
                    dict(url='https://example.org#fragment'), dict(url='http://host:65536'),
                    dict(url='http://host:no'), dict(url='http://[bad]')]]
        invalid += [' '*16385, '{"ssid":"nul\0byte"}']
        for data in invalid:
            with self.subTest(data=data[:100]):
                result = self.command('--settings-set', data, check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn('password12', result.stderr + result.stdout)
                self.assertEqual((self.home / 'config').read_bytes(), saved)
                self.assertFalse((self.home / 'reload').exists())
                self.assertFalse((self.home / 'sync').exists())

    def test_legacy_device_id_is_ignored_and_dropped_on_save(self):
        (self.home / 'config').write_text('ssid=home\npassword=password12\nurl=\ntoken=\ndevice_id=nokia-8110\n')
        self.assertEqual(json.loads(self.command('--settings').stdout),
                         dict(ssid='home', url='', password_set=True, token_set=False))
        self.update({'token': 'new-token'})
        self.assertEqual((self.home / 'config').read_text(),
                         'ssid=home\npassword=password12\nurl=\ntoken=new-token\n')

    def test_corrupt_config_and_busy_lock_are_not_overwritten(self):
        (self.home / 'config').write_text('unrecognized=private\n')
        self.assertNotEqual(self.command('--settings-set', '{"ssid":"home"}', check=False).returncode, 0)
        self.assertEqual((self.home / 'config').read_text(), 'unrecognized=private\n')
        (self.home / 'config').unlink()
        with (self.home / 'settings.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.command('--settings-set', '{"ssid":"home"}', check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('409', result.stderr)
        self.assertFalse((self.home / 'config').exists())

    def test_http_authentication_redaction_updates_and_key_persistence(self):
        self.update(dict(ssid='home', password='secret-password', token='receiver-token'))
        self.start()
        self.assertEqual(self.request(auth=False)[0], 401)
        self.assertEqual(self.request(headers={'Authorization': 'Bearer receiver-token'})[0], 401)
        status, data = self.request()
        self.assertEqual(status, 200)
        self.assertNotIn('secret-password', json.dumps(data))
        self.assertNotIn('receiver-token', json.dumps(data))
        self.assertTrue(data['token_set'])
        status, data = self.request('PATCH', json.dumps(dict(ssid='new-home', url='http://host:8080/sms')))
        self.assertEqual(status, 200)
        self.assertEqual(data['url'], 'http://host:8080/sms')
        self.assertIn('password=secret-password\n', (self.home / 'config').read_text())
        self.assertEqual(self.request('PATCH', '{"token":"new-token"}')[0], 200)
        self.assertEqual(self.request()[1], json.loads(self.command('--settings').stdout))
        self.assertEqual(self.request('PATCH', '{}')[0], 400)
        self.assertEqual(self.request('PATCH', '{"ssid":false}')[0], 400)
        self.assertEqual(self.request('PATCH', '{"ssid":"ok"}', {'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '{}')[0], 405)
        self.assertEqual(self.request(path='/wrong')[0], 404)
        with (self.home / 'settings.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.request('PATCH', '{"ssid":"busy"}')[0], 409)
        self.process.terminate()
        self.process.communicate(timeout=5)
        self.assertEqual(self.command('--settings-key').stdout.strip(), self.key)
        self.assertEqual((self.home / 'settings.key').stat().st_mode & 0o777, 0o600)
        self.start()
        self.assertEqual(self.request()[1]['ssid'], 'new-home')
        self.assertIsNone(self.process.poll())

    def test_http_rejects_ambiguous_or_oversize_requests_and_handles_fragments(self):
        self.start()
        prefix = ('PATCH /v1/settings HTTP/1.1\r\nAuthorization: Bearer ' + self.key +
                  '\r\nContent-Type: application/json\r\n').encode()
        invalid = [(b'Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}', 400),
                   (b'Transfer-Encoding: chunked\r\n\r\n0\r\n\r\n', 400),
                   (b'Origin: http://hostile.example\r\nContent-Length: 2\r\n\r\n{}', 400),
                   (b'Content-Length: 16385\r\n\r\n', 413),
                   (b'Content-Length: -1\r\n\r\n', 400),
                   (b'Content-Length: 3\r\n\r\n{\0}', 400),
                   (b'Content-Length: 2\r\n\r\n{}trailing', 400),
                   (b'Content-Length: 0\r\n\r\n', 400),
                   (b'Authorization: Bearer ' + self.key.encode() + b'\r\n\r\n', 400)]
        for suffix, expected in invalid:
            with self.subTest(suffix=suffix[:80]):
                self.assertEqual(self.raw(prefix + suffix), expected)
        self.assertEqual(self.raw(b'GET /v1/settings HTTP/1.1\r\nX-Large: ' + b'x'*4096), 413)
        for value in [b'\xff', b'\xc0\x80', b'\xed\xa0\x80', b'\xf4\x90\x80\x80', b'\\ud800']:
            body = b'{"ssid":"' + value + b'"}'
            self.assertEqual(self.raw(prefix + f'Content-Length: {len(body)}\r\n\r\n'.encode() + body), 400)
        body = json.dumps({'ssid': 'fragmented'}).encode()
        header = prefix + f'Content-Length: {len(body)}\r\n\r\n'.encode()
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as conn:
            for byte in header:
                conn.sendall(bytes([byte]))
            conn.sendall(body[:2])
            self.assertEqual(self.request()[0], 200)
            conn.sendall(body[2:])
            response = http.client.HTTPResponse(conn)
            response.begin()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['ssid'], 'fragmented')

    def test_stalled_http_client_does_not_block_modem_receipts(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.home / 'rild'))
        server.listen(1)
        server.settimeout(5)
        self.addCleanup(server.close)
        self.start()
        conn, _ = server.accept()
        conn.settimeout(3)
        self.addCleanup(conn.close)
        fixtures.read_frame(conn)  # Initial SIM query, independent of receipt handling.
        with socket.create_connection(('127.0.0.1', self.port), timeout=5) as slow:
            slow.sendall(b'PATCH /v1/settings HTTP/1.1\r\nContent-Length: 100\r\n')
            fixtures.send_frame(conn, struct.pack('<ii', 1, 1003) + fixtures.parcel_string(fixtures.deliver('during HTTP')))
            ack = fixtures.read_frame(conn)
            self.assertEqual(struct.unpack('<i', ack[:4])[0], 37)
            self.assertEqual(struct.unpack('<iii', ack[8:]), (2, 1, 0))
            self.assertEqual(self.request('PATCH', '{"ssid":"still-receiving"}')[0], 200)
        with closing(sqlite3.connect(self.home / 'native.sqlite3')) as db:
            self.assertEqual(db.execute('SELECT body FROM messages').fetchall(), [('during HTTP',)])
        self.assertIsNone(self.process.poll())


class SettingsAdbTests(unittest.TestCase):
    def test_patch_uses_private_staging_and_cleans_up_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'patch.json'
            source.write_text('{"token":"private-token"}')
            with patch('scripts.settings_native.Adb') as factory:
                adb = factory.return_value
                adb.shell.side_effect = ['0\n', '', RuntimeError('rejected patch'), '']
                def push(*args):
                    path = Path(args[1])
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    self.assertEqual(json.loads(path.read_text()), dict(token='private-token'))
                adb.run.side_effect = push
                with self.assertRaisesRegex(RuntimeError, 'rejected patch'):
                    settings_native.execute('set', 'test-phone', source)
                factory.assert_called_once_with('test-phone')
                commands = [call.args[0] for call in adb.shell.call_args_list]
                self.assertIn('--settings-set < ', commands[2])
                self.assertTrue(commands[-1].startswith('rm -f /data/kaiforward/settings-input-'))
                self.assertNotIn('private-token', '\n'.join(commands))
                staged = Path(adb.run.call_args.args[1])
            self.assertFalse(staged.exists())

    def test_invalid_json_does_not_connect_to_phone(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'patch.json'
            for value in [[], {}, {'password': None}, {'unexpected': 'value'}]:
                source.write_text(json.dumps(value))
                with patch('scripts.settings_native.Adb') as factory:
                    with self.assertRaises(ValueError):
                        settings_native.execute('set', source=source)
                    factory.assert_not_called()
            source.write_text('{"ssid":"one","ssid":"two"}')
            with patch('scripts.settings_native.Adb') as factory:
                with self.assertRaisesRegex(ValueError, 'Duplicate'):
                    settings_native.execute('set', source=source)
                factory.assert_not_called()

    def test_reads_are_redacted_and_non_root_phone_is_rejected(self):
        with patch('scripts.settings_native.Adb') as factory:
            adb = factory.return_value
            adb.shell.side_effect = ['0\n', '{"ssid":"home"}\n']
            self.assertEqual(settings_native.execute('get', 'test'), '{"ssid":"home"}\n')
            self.assertEqual(adb.shell.call_args.args[0], settings_native.RUNTIME + ' --settings')
            adb.shell.side_effect = ['2000\n']
            with self.assertRaisesRegex(ValueError, 'root ADB'):
                settings_native.execute('key', 'test')
