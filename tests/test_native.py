"""Native forwarding contract tests; synthetic messages, sockets, and receiver."""
import ctypes
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ROOT / 'firmware/native'


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


receiver = load_module('native_receiver', ROOT / 'tests/support/receiver.py')
from scripts import configure_native as configure


def deliver(text='hello', sender='+8801712345678', multipart=None, gsm=False):
    digits = sender.lstrip('+')
    padded = digits + ('F' if len(digits) % 2 else '')
    address = bytes.fromhex(''.join(padded[i + 1] + padded[i] for i in range(0, len(padded), 2)))
    header = bytes([5, 0, 3, *multipart]) if multipart else b''
    if gsm:
        # Independent GSM packing of default ASCII and the escape-table euro.
        values = []
        for ch in text:
            values.extend([27, 101] if ch == '€' else [ord(ch)])
        skipped = (len(header) * 8 + 6) // 7
        packed = int.from_bytes(header, 'little')
        for i, val in enumerate(values):
            packed |= val << ((skipped + i) * 7)
        udl = skipped + len(values)
        body = packed.to_bytes((udl * 7 + 7) // 8, 'little')
    else:
        body = header + text.encode('utf-16-be')
        udl = len(body)
    # 2026-10-13 12:34:56, +06:00 (24 quarter hours).
    stamp = bytes.fromhex('62013121436542')
    pdu = bytes([0, 64 if header else 0, len(digits), 0x91 if sender.startswith('+') else 0x81])
    return (pdu + address + bytes([0, 0 if gsm else 8]) + stamp + bytes([udl]) + body).hex().upper()


def parcel_string(value):
    if value is None:
        return struct.pack('<i', -1)
    data = struct.pack('<i', len(value)) + value.encode('utf-16-le') + b'\0\0'
    return data + b'\0' * (-len(data) % 4)


def send_frame(conn, data, fragmented=False):
    frame = struct.pack('>I', len(data)) + data
    if fragmented:
        for byte in frame:
            conn.sendall(bytes([byte]))
    else:
        conn.sendall(frame)


def read_frame(conn):
    def exact(n):
        out = b''
        while len(out) < n:
            part = conn.recv(n - len(out))
            if not part:
                raise EOFError
            out += part
        return out
    return exact(struct.unpack('>I', exact(4))[0])


def eventually(check, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.04)
    raise AssertionError('Timed out waiting for native behavior')


class NativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.build.name) / 'native'
        cls.lib_path = Path(cls.build.name) / 'native.so'
        flags = ['gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-misleading-indentation', '-I' + str(SOURCES)]
        subprocess.run(flags + [str(p) for p in sorted(SOURCES.glob('*.c'))] + ['-lsqlite3', '-o', str(cls.binary)], check=True)
        sources = [SOURCES / name for name in ['common.c', 'sms.c', 'submission.c', 'store.c', 'wifi.c', 'radio.c', 'permission.c', 'call.c']]
        subprocess.run(flags + ['-shared', '-fPIC', *map(str, sources), '-lsqlite3', '-o', str(cls.lib_path)], check=True)
        cls.lib = ctypes.CDLL(str(cls.lib_path))
        cls.lib.store_open.argtypes = [ctypes.c_char_p]
        cls.lib.store_open.restype = ctypes.c_void_p
        cls.lib.store_pdu.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p]
        cls.lib.store_pdu.restype = ctypes.c_int
        cls.lib.ack_valid.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.lib.ack_valid.restype = ctypes.c_int
        cls.lib.sqlite3_close.argtypes = [ctypes.c_void_p]

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = self.lib.store_open(os.fsencode(self.home))
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.lib.sqlite3_close(self.db))

    def rows(self, sql='SELECT sender,body,slot FROM messages'):
        with closing(sqlite3.connect(self.home / 'native.sqlite3')) as db:
            return db.execute(sql).fetchall()

    def add(self, pdu, slot=1):
        self.assertEqual(self.lib.store_pdu(self.db, slot, pdu.encode()), 0)

    def process(self, *args):
        process = subprocess.Popen([str(self.binary), '--home', str(self.home), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def close():
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
            else:
                process.communicate()
        self.addCleanup(close)
        return process

    def test_unicode_and_service_centre_timezone(self):
        text = 'বাংলা SMS 😀'
        result = subprocess.run([str(self.binary), '--decode', deliver(text)], capture_output=True, check=True, text=True)
        decoded = json.loads(result.stdout)
        self.assertEqual(decoded['body'], text)
        self.assertEqual(decoded['sender'], '+8801712345678')
        self.assertEqual(decoded['timestamp'], 1791873296000)

    def test_gsm_escape_alphabet_and_multipart_padding(self):
        self.add(deliver('hello €', gsm=True))
        self.add(deliver('world', multipart=(55, 2, 2), gsm=True))
        self.add(deliver('hello ', multipart=(55, 2, 1), gsm=True))
        self.assertEqual([r[1] for r in self.rows()], ['hello €', 'hello world'])

    def test_dual_sim_dedup_reopen_and_incomplete_segments(self):
        pdu = deliver('hello')
        self.add(pdu)
        self.add(pdu)
        self.add(pdu, 2)
        self.add(deliver('part', multipart=(9, 2, 2)))
        self.assertEqual(len(self.rows()), 2)
        self.lib.sqlite3_close(self.db)
        self.db = self.lib.store_open(os.fsencode(self.home))
        self.add(deliver('first ', multipart=(9, 2, 1)))
        self.assertEqual(self.rows()[-1][1], 'first part')
        self.assertEqual([row[2] for row in self.rows()[:2]], [1, 2])

    def test_unsupported_and_conflicting_parts_remain_raw(self):
        self.add('00FF')
        self.add(deliver('first', multipart=(8, 2, 1)))
        self.add(deliver('conflict', multipart=(8, 2, 1)))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.rows('SELECT COUNT(*) FROM raw WHERE decoded=0')[0][0], 2)

    def test_storage_failure_does_not_leave_partial_message(self):
        with closing(sqlite3.connect(self.home / 'native.sqlite3')) as db:
            db.execute("CREATE TRIGGER no_write BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'fault injection'); END")
        self.assertEqual(self.lib.store_pdu(self.db, 1, deliver().encode()), -1)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM raw')[0][0], 0)
        with closing(sqlite3.connect(self.home / 'native.sqlite3')) as db:
            db.execute('DROP TRIGGER no_write')
        self.add(deliver())
        self.assertEqual(len(self.rows()), 1)

    def test_acknowledgement_must_match_exact_message(self):
        key = b'1000000001:1791873296000'
        self.assertEqual(self.lib.ack_valid(b'{ "accepted" : [ "1000000001:1791873296000" ] }', key), 1)
        for bad in [b'{}', b'{"accepted":[]}', b'{"accepted":["wrong"]}', b'{"accepted":["1000000001:1791873296000","extra"]}', b'{"accepted":["1000000001:1791873296000"]}trailing']:
            self.assertEqual(self.lib.ack_valid(bad, key), 0)

    def test_relay_address_from_relay_url(self):
        def address(url):
            host, port = ctypes.create_string_buffer(256), ctypes.create_string_buffer(8)
            if self.lib.relay_address(url.encode(), host, 256, port, 8):
                return None
            return host.value.decode(), port.value.decode()
        self.assertEqual(address('tls://100.64.0.1'), ('100.64.0.1', '8769'))
        self.assertEqual(address('tls://relay.tailnet.ts.net:9000'), ('relay.tailnet.ts.net', '9000'))
        self.assertEqual(address('tls://[fd7a:115c:a1e0::1]:9000'), ('fd7a:115c:a1e0::1', '9000'))
        self.assertEqual(address('tls://[fd7a:115c:a1e0::1]'), ('fd7a:115c:a1e0::1', '8769'))
        for bad in ['', 'https://host', 'tls://', 'tls://host/', 'tls://host:', 'tls://host:99999']:
            self.assertIsNone(address(bad), bad)

    def test_native_settings_and_wifi_escaping(self):
        values = dict(ssid='test"wifi', password='password"12', url='https://example.org/v1/messages', token='test-token')
        encoded = configure.encode_config(values)
        (self.home / 'config').write_text(encoded)
        result = subprocess.run([str(self.binary), '--home', str(self.home), '--wifi-config'], capture_output=True, text=True, check=True)
        self.assertIn('ssid="test\\"wifi"', result.stdout)
        self.assertIn('psk="password\\"12"', result.stdout)
        self.assertIn('relay_url=\n', encoded)
        self.assertIn('relay_url=tls://relay.example:9000\n', configure.encode_config(dict(values, relay_url='tls://relay.example:9000')))
        for name, value in [('password', 'short'), ('url', 'file:///tmp/x'), ('token', 'bad\nheader'), ('ssid', 'x'*33),
                            ('relay_url', 'https://relay.example'), ('relay_url', 'tls://relay.example/path'), ('relay_url', 'tls://relay.example:70000')]:
            bad = dict(values, **{name: value})
            with self.assertRaises(ValueError):
                configure.encode_config(bad)
        self.assertEqual(subprocess.run([str(self.binary), '--network-prefix', '192.168.0.122', '255.255.255.0'], capture_output=True, text=True, check=True).stdout, '192.168.0.0/24\n')
        self.assertNotEqual(subprocess.run([str(self.binary), '--network-prefix', '192.168.0.122', '255.0.255.0'], capture_output=True).returncode, 0)

    def test_wifi_scan_without_credentials_and_control_socket(self):
        result = subprocess.run([str(self.binary), '--home', str(self.home), '--wifi-config'],
                                capture_output=True, text=True, check=True)
        self.assertIn('ctrl_interface=', result.stdout)
        self.assertNotIn('network={', result.stdout)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        path = self.home / 'wlan0';server.bind(str(path));server.settimeout(3)
        self.addCleanup(server.close)
        commands = []
        def respond():
            for _ in range(2):
                command, sender = server.recvfrom(1024);commands.append(command)
                response = (b'bssid / frequency / signal level / flags / ssid\n'
                            b'aa:00:00:00:00:01\t2412\t-75\t[WPA2-PSK-CCMP][ESS]\tCafe\\x20WiFi\n'
                            b'aa:00:00:00:00:02\t2412\t-40\t[WPA2-PSK-CCMP][ESS]\tCafe\\x20WiFi\n'
                            b'aa:00:00:00:00:03\t2412\t-60\t[ESS]\tOpen network\n'
                            b'aa:00:00:00:00:04\t2412\t-70\t[WPA2-EAP-CCMP][ESS]\tEnterprise\n'
                            b'aa:00:00:00:00:05\t2412\t-20\t[ESS]\tBad\\x0aname\n'
                            if command == b'SCAN_RESULTS' else b'OK\n')
                server.sendto(response, sender)
        thread = threading.Thread(target=respond);thread.start()
        self.lib.wifi_scan.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        self.lib.wifi_scan.restype = ctypes.c_int
        self.assertEqual(self.lib.wifi_scan(os.fsencode(self.home), os.fsencode(path)), 0)
        thread.join(3)
        self.assertEqual(commands, [b'SCAN_RESULTS', b'SCAN'])
        self.assertEqual((self.home / 'wifi.scan').read_text(),
                         '-40\t1\tCafe WiFi\n-60\t0\tOpen network\n-70\t2\tEnterprise\n')
        self.assertEqual(list(self.home.glob('wlan0.kf-*')), [])

    def test_absent_sim_clears_previous_registration(self):
        _, conn, _ = self.fake_modem()
        send_frame(conn, struct.pack('<ii', 1, 1019))
        request = read_frame(conn)
        kind, token = struct.unpack('<ii', request[:8]);self.assertEqual(kind, 1)
        absent = struct.pack('<iiiiii', 0, 0, -1, -1, -1, 0)
        send_frame(conn, struct.pack('<iii', 0, token, 0) + absent)
        eventually(lambda: 'SIM 1: absent' in (self.home / 'radio.status').read_text(), timeout=7)
        self.assertEqual(self.queue_send().returncode, 0)
        conn.settimeout(.3)
        with self.assertRaises(socket.timeout):
            read_frame(conn)
        self.assertEqual(self.rows('SELECT status FROM outbox'), [('queued',)])

    def test_fragmented_modem_frames_ack_after_durable_storage(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.home / 'rild'))
        server.listen(1)
        server.settimeout(5)
        self.addCleanup(server.close)
        process = self.process('--socket-dir', str(self.home))
        conn, _ = server.accept()
        conn.settimeout(5)
        self.addCleanup(conn.close)
        initial = read_frame(conn)  # Initial SIM status request.
        token = struct.unpack('<i', initial[4:8])[0]
        card = struct.pack('<iiiiii', 1, 0, 0, -1, -1, 1)
        card += struct.pack('<iii', 2, 5, 0) + parcel_string('A000000') + parcel_string('রবি') + struct.pack('<iii', 0, 2, 2)
        send_frame(conn, struct.pack('<iii', 0, token, 0) + card)
        payload = struct.pack('<ii', 1, 1003) + parcel_string(deliver('durable'))
        send_frame(conn, payload, fragmented=True)
        request = read_frame(conn)
        self.assertEqual(struct.unpack('<i', request[:4])[0], 37)
        self.assertEqual(struct.unpack('<iii', request[8:]), (2, 1, 0))
        self.assertEqual(self.rows()[0][1], 'durable')
        token = struct.unpack('<i', request[4:8])[0]
        send_frame(conn, struct.pack('<iii', 0, token, 0))
        # A failed queue commit must produce a negative ACK, not a positive one.
        with closing(sqlite3.connect(self.home / 'native.sqlite3')) as db:
            db.execute("CREATE TRIGGER no_write BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        send_frame(conn, struct.pack('<ii', 1, 1003) + parcel_string(deliver('cannot save')))
        request = read_frame(conn)
        self.assertEqual(struct.unpack('<iii', request[8:]), (2, 0, 0xd3))
        self.assertEqual(self.rows('SELECT COUNT(*) FROM raw')[0][0], 1)
        self.assertIsNone(process.poll())

    def test_invalid_frame_is_rejected_without_receipt(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.home / 'rild'));server.listen(1);server.settimeout(5)
        self.addCleanup(server.close)
        self.process('--socket-dir', str(self.home))
        conn, _ = server.accept();self.addCleanup(conn.close);conn.settimeout(5)
        read_frame(conn)
        conn.sendall(struct.pack('>I', 100000))
        self.assertEqual(conn.recv(1), b'')
        self.assertEqual(self.rows(), [])

    def test_upload_to_real_receiver_replays_after_lost_ack(self):
        config = {'token': 'test-token'}
        receiver_path = self.home / 'receiver.sqlite3'
        receiver.connect(receiver_path).close()
        handler = receiver.handler_for(receiver_path, config)
        class LostAck(handler):
            lose = True
            def respond(inner, status, payload):
                if status == 200 and inner.path == '/v1/messages' and LostAck.lose:
                    LostAck.lose = False
                    payload = {'accepted': []}
                return super().respond(status, payload)
        server = ThreadingHTTPServer(('127.0.0.1', 0), LostAck)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        values = dict(ssid='test', password='password12', url=f'http://127.0.0.1:{server.server_port}/v1/messages', **config)
        (self.home / 'config').write_text(configure.encode_config(values))
        self.add(deliver('queued বাংলা'))
        process = self.process('--upload', '--curl', '/usr/bin/curl')
        eventually(lambda: 'RETRYING' in (self.home / 'upload.status').read_text() if (self.home / 'upload.status').exists() else False)
        self.assertEqual(self.rows('SELECT uploaded FROM messages'), [(0,)])
        (self.home / 'sync').touch()
        eventually(lambda: self.rows('SELECT uploaded FROM messages') == [(1,)])
        with closing(sqlite3.connect(receiver_path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
            saved = json.loads(db.execute('SELECT payload FROM messages').fetchone()[0])
            self.assertEqual(saved['body'], 'queued বাংলা')
            self.assertEqual(saved['sim_slot'], 1)
        self.assertFalse((self.home / 'curl.conf').exists())
        self.assertIsNone(process.poll())

    def test_native_render_has_phone_dimensions(self):
        output = self.home / 'screen.ppm'
        subprocess.run([str(self.binary), '--home', str(self.home), '--preview', str(output)], check=True)
        data = output.read_bytes()
        self.assertTrue(data.startswith(b'P6\n240 320\n255\n'))
        self.assertGreater(len(set(data[15:])), 2)

    def ready_modem(self, conn):
        card = struct.pack('<iiiiii', 1, 0, 0, -1, -1, 1)
        card += struct.pack('<iii', 2, 5, 0) + parcel_string('A000000') + parcel_string('Robi') + struct.pack('<iii', 0, 2, 2)
        initial = read_frame(conn)
        send_frame(conn, struct.pack('<iii', 0, struct.unpack('<i', initial[4:8])[0], 0) + card)
        send_frame(conn, struct.pack('<iii', 1, 1000, 10))
        for _ in range(2):
            request = read_frame(conn)
            kind, token = struct.unpack('<ii', request[:8])
            self.assertIn(kind, (1, 20))
            result = card if kind == 1 else struct.pack('<i', 1) + parcel_string('1')
            send_frame(conn, struct.pack('<iii', 0, token, 0) + result)

    def fake_modem(self):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.home / 'rild'));server.listen(1);server.settimeout(5)
        self.addCleanup(server.close)
        process = self.process('--socket-dir', str(self.home))
        conn, _ = server.accept();conn.settimeout(5);self.addCleanup(conn.close)
        self.ready_modem(conn)
        return server, conn, process

    def test_permission_controller_grants_only_stack_audio_settings(self):
        self.lib.permission_allowed.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
        self.lib.permission_reply.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        permission = b'android.permission.MODIFY_AUDIO_SETTINGS'
        self.assertEqual(self.lib.permission_allowed(permission, 1013, 1001), 1)
        for caller, uid in [(2000, 1001), (10001, 1001), (1013, 10001), (1013, -1)]:
            self.assertEqual(self.lib.permission_allowed(permission, caller, uid), 0)
        for name in [b'android.permission.RECORD_AUDIO', b'android.permission.CAMERA', b'android.permission.MODIFY_AUDIO_ROUTING']:
            self.assertEqual(self.lib.permission_allowed(name, 1013, 1001), 0)
        payload = struct.pack('<i', 0) + parcel_string('android.os.IPermissionController')
        payload += parcel_string(permission.decode()) + struct.pack('<ii', 123, 1001)
        reply = ctypes.create_string_buffer(128)
        length = self.lib.permission_reply(1, 1013, payload, len(payload), reply, len(reply))
        self.assertEqual(length, 8)
        self.assertEqual(struct.unpack('<ii', reply.raw[:8]), (0, 1))
        length = self.lib.permission_reply(1, 2000, payload, len(payload), reply, len(reply))
        self.assertEqual(length, 8)
        self.assertEqual(struct.unpack('<ii', reply.raw[:8]), (0, 0))
        for bad in [payload[:-1], payload + b'extra', struct.pack('<i', 0) + parcel_string('wrong')]:
            self.assertEqual(self.lib.permission_reply(1, 1013, bad, len(bad), reply, len(reply)), -1)

    def test_incoming_call_status_and_sms_receipt_count(self):
        _, conn, _ = self.fake_modem()
        send_frame(conn, struct.pack('<ii', 1, 1001))
        request = read_frame(conn)
        code, token = struct.unpack('<ii', request[:8]);self.assertEqual(code, 9)
        send_frame(conn, struct.pack('<iii', 0, token, 0) + struct.pack('<iii', 1, 4, 1))
        eventually(lambda: 'Call: SIM 1 incoming' in (self.home / 'radio.status').read_text(), timeout=7)
        send_frame(conn, struct.pack('<ii', 1, 1003) + parcel_string(deliver('receipt indicator')))
        ack = read_frame(conn);self.assertEqual(struct.unpack_from('<i', ack)[0], 37)
        send_frame(conn, struct.pack('<iii', 0, struct.unpack_from('<i', ack, 4)[0], 0))
        eventually(lambda: 'Received: 1' in (self.home / 'radio.status').read_text(), timeout=7)
        send_frame(conn, struct.pack('<ii', 1, 1001))
        request = read_frame(conn);token = struct.unpack_from('<i', request, 4)[0]
        send_frame(conn, struct.pack('<iiii', 0, token, 0, 0))
        eventually(lambda: 'Call: idle' in (self.home / 'radio.status').read_text(), timeout=7)
        events = (self.home / 'radio.events').read_text()
        self.assertIn('event code=1003', events)
        self.assertNotIn('receipt indicator', events)
        self.assertNotIn('8801712345678', events)

    def queue_send(self, body='native বাংলা', slot='1'):
        return subprocess.run([str(self.binary), '--home', str(self.home), '--send', slot, '+8801712345678', body], capture_output=True, text=True)

    def call_state(self, conn, count, state=0, index=1):
        send_frame(conn, struct.pack('<ii', 1, 1001))
        query = read_frame(conn)
        self.assertEqual(struct.unpack_from('<i', query)[0], 9)
        payload = struct.pack('<i', count)
        if count:
            payload += struct.pack('<ii', state, index)
        send_frame(conn, struct.pack('<iii', 0, struct.unpack_from('<i', query, 4)[0], 0) + payload)
        # Same socket ordering ensures the query reply is processed before a
        # later status request; retry status until it reflects the new state.
        def current():
            result = subprocess.run([str(self.binary), '--home', str(self.home), '--call', '1', 'status'], capture_output=True, text=True)
            return result.returncode == 0 and json.loads(result.stdout)['state'] == (state if count else -1)
        eventually(current)

    def test_call_control_answers_once_and_keeps_receiving_sms(self):
        _, conn, _ = self.fake_modem()
        self.call_state(conn, 1, 4, 3)
        command = self.process('--call', '1', 'answer')
        request = read_frame(conn)
        self.assertEqual(struct.unpack_from('<i', request)[0], 40)
        self.assertEqual(len(request), 8)
        send_frame(conn, struct.pack('<ii', 1, 1003) + parcel_string(deliver('SMS during answer')))
        ack = read_frame(conn)
        self.assertEqual(struct.unpack_from('<i', ack)[0], 37)
        send_frame(conn, struct.pack('<iii', 0, struct.unpack_from('<i', ack, 4)[0], 0))
        send_frame(conn, struct.pack('<iii', 0, struct.unpack_from('<i', request, 4)[0], 0))
        output, _ = command.communicate(timeout=3)
        self.assertEqual(command.returncode, 0)
        self.assertTrue(json.loads(output)['ok'])
        query = read_frame(conn)
        self.assertEqual(struct.unpack_from('<i', query)[0], 9)
        send_frame(conn, struct.pack('<iii', 0, struct.unpack_from('<i', query, 4)[0], 0) + struct.pack('<iii', 1, 0, 3))
        eventually(lambda: len(self.rows()) == 1)

    def test_call_hangup_uses_exact_index_and_disconnect_does_not_retry(self):
        _, conn, _ = self.fake_modem()
        self.call_state(conn, 1, 0, 3)
        command = self.process('--call', '1', 'hangup')
        request = read_frame(conn)
        self.assertEqual(struct.unpack_from('<i', request)[0], 12)
        self.assertEqual(request[8:], struct.pack('<ii', 1, 3))
        conn.close()
        output, _ = command.communicate(timeout=3)
        self.assertNotEqual(command.returncode, 0)
        self.assertTrue(json.loads(output)['uncertain'])
        events = (self.home / 'radio.events').read_text()
        self.assertEqual(events.count('request code=12'), 1)

    def test_call_control_refuses_invalid_state_number_and_multiple_calls(self):
        _, conn, _ = self.fake_modem()
        self.call_state(conn, 2, 4, 3)
        for args in [('answer',), ('hangup',), ('dial', '+8801712345678'), ('dial', '*123#'), ('dial', 'x')]:
            result = subprocess.run([str(self.binary), '--home', str(self.home), '--call', '1', *args], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.home / 'call.sock').stat().st_mode & 0o777, 0o600)
        conn.settimeout(.1)
        with self.assertRaises(socket.timeout):
            conn.recv(1)

    def test_native_send_archives_only_modem_confirmed_success(self):
        self.assertEqual(self.queue_send().returncode, 0)
        _, conn, _ = self.fake_modem()
        request = read_frame(conn)
        self.assertEqual(struct.unpack('<i', request[:4])[0], 25)
        self.assertEqual(request[8:16], struct.pack('<ii', 2, -1))
        self.assertEqual(self.rows(), [])
        token = struct.unpack('<i', request[4:8])[0]
        result = struct.pack('<i', 12) + parcel_string(None) + struct.pack('<i', 0)
        send_frame(conn, struct.pack('<iii', 0, token, 0) + result)
        eventually(lambda: len(self.rows()) == 1)
        self.assertEqual(self.rows('SELECT direction,recipient,body FROM messages'), [('sent', '+8801712345678', 'native বাংলা')])
        self.assertEqual(self.rows('SELECT status FROM outbox'), [('sent',)])

    def test_native_send_disconnect_is_uncertain_without_resend(self):
        self.assertEqual(self.queue_send().returncode, 0)
        _, conn, process = self.fake_modem()
        request = read_frame(conn)
        self.assertEqual(struct.unpack('<i', request[:4])[0], 25)
        conn.close()
        eventually(lambda: self.rows('SELECT status FROM outbox') == [('uncertain',)])
        self.assertEqual(self.rows(), [])
        process.terminate();process.communicate(timeout=5)
        self.process('--socket-dir', str(self.home))
        time.sleep(.1)
        self.assertEqual(self.rows('SELECT status FROM outbox'), [('uncertain',)])

    def test_native_send_refuses_invalid_or_overlong_commands(self):
        for body, slot in [('x'*71, '1'), ('valid', '3'), ('', '1')]:
            self.assertNotEqual(self.queue_send(body, slot).returncode, 0)
        self.assertEqual(self.rows('SELECT COUNT(*) FROM outbox'), [(0,)])

    def test_https_upload_requires_trusted_certificate(self):
        certificate, private_key = self.home / 'certificate.pem', self.home / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-keyout', str(private_key), '-out', str(certificate), '-subj', '/CN=127.0.0.1',
                        '-addext', 'subjectAltName=IP:127.0.0.1'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        config = {'token': 'test-token'}
        path = self.home / 'tls-receiver.sqlite3';receiver.connect(path).close()
        server = ThreadingHTTPServer(('127.0.0.1', 0), receiver.handler_for(path, config))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certificate, private_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        values = dict(ssid='test', password='password12', url=f'https://127.0.0.1:{server.server_port}/v1/messages', **config)
        (self.home / 'config').write_text(configure.encode_config(values))
        self.add(deliver('TLS'))
        self.process('--upload', '--curl', '/usr/bin/curl')
        eventually(lambda: (self.home / 'upload.status').exists())
        self.assertEqual(self.rows('SELECT uploaded FROM messages'), [(0,)])
        (self.home / 'ca.pem').write_bytes(certificate.read_bytes())
        (self.home / 'sync').touch()
        eventually(lambda: self.rows('SELECT uploaded FROM messages') == [(1,)])


if __name__ == '__main__':
    unittest.main()
