import http.client
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from receiver import connect, handler_for


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / 'test.sqlite3'
        connect(self.database).close()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.database, {'token': 'test-secret', 'device_id': 'phone'}))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def send(self, messages, token='test-secret', device='phone'):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('POST', '/v1/messages', json.dumps({'device_id': device, 'messages': messages}),
                           {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def message(self, ident=1, body='বাংলা SMS ✓'):
        return {'key': str(ident) + ':1720000000000', 'id': ident, 'timestamp': 1720000000000, 'sender': '+880123456789', 'body': body}

    def count(self):
        with sqlite3.connect(self.database) as database:
            return database.execute('SELECT COUNT(*) FROM messages').fetchone()[0]

    def test_unicode_replay_is_idempotent(self):
        record = self.message()
        self.assertEqual(self.send([record]), (200, {'accepted': [record['key']]}))
        self.assertEqual(self.send([record])[0], 200)
        self.assertEqual(self.count(), 1)
        with sqlite3.connect(self.database) as database:
            saved = json.loads(database.execute('SELECT payload FROM messages').fetchone()[0])
        self.assertEqual(saved['body'], record['body'])

    def test_authentication_and_device_binding(self):
        self.assertEqual(self.send([self.message()], token='wrong')[0], 401)
        self.assertEqual(self.send([self.message()], device='other')[0], 400)
        self.assertEqual(self.count(), 0)

    def test_sent_sms_preserves_recipient_and_direction_on_replay(self):
        record = {**self.message(), 'sender': '', 'recipient': '+880456', 'direction': 'sent'}
        self.assertEqual(self.send([record])[0], 200)
        self.assertEqual(self.send([record])[0], 200)
        with sqlite3.connect(self.database) as database:
            saved = json.loads(database.execute('SELECT payload FROM messages').fetchone()[0])
        self.assertEqual(saved, record)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.send([{**record, 'recipient': '+880999'}])[0], 409)

    def test_legacy_received_record_replays_with_direction_defaults(self):
        record = self.message()
        with sqlite3.connect(self.database) as database:
            database.execute('INSERT INTO messages VALUES (?,?,?,?)', ('phone', record['key'], json.dumps(record, sort_keys=True, ensure_ascii=False), 1))
        self.assertEqual(self.send([{**record, 'direction': 'received', 'recipient': ''}])[0], 200)
        self.assertEqual(self.send([record])[0], 200)
        self.assertEqual(self.count(), 1)

    def test_invalid_directions_and_sent_recipients_are_rejected(self):
        for fields in ({'direction':'sending'}, {'direction':'sent'}, {'direction':'sent','recipient':123}, {'direction':'sent','recipient':'x'*129}):
            self.assertEqual(self.send([{**self.message(), **fields}])[0], 400)
        self.assertEqual(self.count(), 0)

    def test_conflict_rolls_back_entire_batch(self):
        self.send([self.message()])
        status, _ = self.send([self.message(2), self.message(body='different')])
        self.assertEqual(status, 409)
        self.assertEqual(self.count(), 1)

    def test_invalid_key_and_duplicates_rejected(self):
        invalid = self.message()
        invalid['key'] = 'forged'
        self.assertEqual(self.send([invalid])[0], 400)
        self.assertEqual(self.send([self.message(), self.message()])[0], 400)
        self.assertEqual(self.count(), 0)

    def test_timestamp_key_prevents_reused_id_collision(self):
        old = self.message()
        new = self.message(body='after phone reset')
        new['timestamp'] += 1000
        new['key'] = '1:' + str(new['timestamp'])
        self.assertEqual(self.send([old, new])[0], 200)
        self.assertEqual(self.count(), 2)

    def test_batch_size_limit(self):
        self.assertEqual(self.send([self.message(ident) for ident in range(51)])[0], 400)
        self.assertEqual(self.count(), 0)

    def test_native_sim_slot_is_preserved_and_validated(self):
        record = {**self.message(), 'sim_slot': 2}
        self.assertEqual(self.send([record])[0], 200)
        self.assertEqual(self.send([record])[0], 200)
        with sqlite3.connect(self.database) as database:
            saved = json.loads(database.execute('SELECT payload FROM messages').fetchone()[0])
        self.assertEqual(saved['sim_slot'], 2)
        for slot in (0, 3, '1', True):
            self.assertEqual(self.send([{**self.message(2), 'sim_slot': slot}])[0], 400)


if __name__ == '__main__':
    unittest.main()
