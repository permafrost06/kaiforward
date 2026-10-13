import http.client
from contextlib import closing
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from receiver import connect
from viewer import handler_for, read_messages


class ViewerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / 'test.sqlite3'
        with closing(connect(self.database)) as database, database:
            for ident, sender, body in [(1, 'Alice', 'hello বাংলা'), (2, 'BANK', 'rate is 5%'), (3, 'Alice', '<script>alert(1)</script>')]:
                record = {'key': str(ident) + ':' + str(ident * 1000), 'id': ident, 'timestamp': ident * 1000, 'sender': sender, 'body': body}
                database.execute('INSERT INTO messages VALUES (?,?,?,?)', ('phone', record['key'], json.dumps(record), ident))
            database.execute('INSERT INTO messages VALUES (?,?,?,?)', ('other-phone', '4:4000', json.dumps({'key':'4:4000','id':4,'timestamp':4000,'sender':'Other','body':'private'}), 4))
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler_for(self.database, 'phone'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def get(self, path, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('GET', path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_newest_first_pagination_and_device_isolation(self):
        result = read_messages(self.database, 'phone', limit=1, offset=1)
        self.assertEqual(result['total'], 3)
        self.assertEqual(result['matched'], 3)
        self.assertEqual([record['id'] for record in result['messages']], [2])

    def test_search_sender_unicode_and_literal_wildcards(self):
        self.assertEqual(read_messages(self.database, 'phone', 'alice')['matched'], 2)
        self.assertEqual(read_messages(self.database, 'phone', 'বাংলা')['matched'], 1)
        self.assertEqual(read_messages(self.database, 'phone', '%')['matched'], 1)
        self.assertEqual(read_messages(self.database, 'phone', "' OR 1=1 --")['matched'], 0)

    def test_sent_recipient_search_sort_filter_and_legacy_received_defaults(self):
        record = {'key':'5:5000', 'id':5, 'timestamp':5000, 'sender':'', 'recipient':'Zoe', 'direction':'sent', 'body':'outgoing'}
        with closing(connect(self.database)) as database, database:
            database.execute('INSERT INTO messages VALUES (?,?,?,?)', ('phone', record['key'], json.dumps(record), 5))
        result = read_messages(self.database, 'phone', 'Zoe')
        self.assertEqual(result['matched'], 1)
        self.assertEqual(result['messages'][0]['recipient'], 'Zoe')
        self.assertEqual(read_messages(self.database, 'phone', sort='sender_desc')['messages'][0]['id'], 5)
        status, body, _ = self.get('/api/messages?direction=sent&limit=1')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['matched'], 1)
        self.assertEqual(json.loads(body)['total'], 4)
        incoming = read_messages(self.database, 'phone', direction='received')
        self.assertEqual(incoming['matched'], 3)
        self.assertTrue(all(message['direction'] == 'received' and message['recipient'] == '' for message in incoming['messages']))
        self.assertEqual(self.get('/api/messages?direction=invalid')[0], 400)

    def test_browser_api_serves_data_and_security_headers(self):
        status, body, headers = self.get('/api/messages?limit=2')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['messages'][0]['body'], '<script>alert(1)</script>')
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Cross-Origin-Resource-Policy'], 'same-origin')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])

    def test_number_prefix_filter_composes_with_direction_search_sort_and_pagination(self):
        records = [
            {'id':5, 'sender':'+8801700000000', 'body':'match'},
            {'id':6, 'sender':'01700000000', 'body':'match'},
            {'id':7, 'sender':'BANK', 'body':'+880 and 01 only in the body'},
            {'id':8, 'sender':'x+8801700000000', 'body':'exclude'},
            {'id':9, 'sender':'00170000000', 'body':'exclude'},
            {'id':10, 'sender':'', 'recipient':'+8801800000000', 'direction':'sent', 'body':'match'},
            {'id':11, 'sender':'+8801700000000', 'recipient':'+441234', 'direction':'sent', 'body':'exclude'},
        ]
        with closing(connect(self.database)) as database, database:
            for record in records:
                record.update(key=str(record['id'])+':'+str(record['id']*1000), timestamp=record['id']*1000)
                database.execute('INSERT INTO messages VALUES (?,?,?,?)', ('phone', record['key'], json.dumps(record), record['id']))
        result = read_messages(self.database, 'phone', number_filter='bd_prefix', sort='oldest')
        self.assertEqual([message['id'] for message in result['messages']], [5, 6, 10])
        self.assertEqual(result['total'], 10)
        self.assertEqual(result['matched'], 3)
        self.assertEqual(read_messages(self.database, 'phone', number_filter='bd_prefix', sort='oldest', limit=1, offset=1)['messages'][0]['id'], 6)
        self.assertEqual(read_messages(self.database, 'phone', number_filter='bd_prefix', direction='received')['matched'], 2)
        status, body, _ = self.get('/api/messages?number_filter=bd_prefix&direction=sent&q=match')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['number_filter'], 'bd_prefix')
        self.assertEqual([message['id'] for message in json.loads(body)['messages']], [10])
        for value in ('unknown', '%25', "all%27%20OR%201=1"):
            self.assertEqual(self.get('/api/messages?number_filter='+value)[0], 400)

    def test_external_host_and_cross_origin_requests_rejected(self):
        self.assertEqual(self.get('/api/messages', {'Host':'attacker.example:'+str(self.server.server_port)})[0], 403)
        self.assertEqual(self.get('/api/messages', {'Origin':'https://attacker.example'})[0], 403)
        self.assertEqual(self.get('/api/messages', {'Sec-Fetch-Site':'cross-site'})[0], 403)

    def test_bad_pagination_and_unknown_routes(self):
        for query in ('limit=0', 'limit=999', 'offset=-1', 'offset=nope', 'sort=invalid', 'sort=timestamp;DROP%20TABLE%20messages'):
            self.assertEqual(self.get('/api/messages?'+query)[0], 400)
        self.assertEqual(self.get('/../../data/config.json')[0], 404)

    def test_sorting_applies_before_pagination(self):
        self.assertEqual([message['id'] for message in read_messages(self.database, 'phone', sort='oldest')['messages']], [1, 2, 3])
        self.assertEqual([message['id'] for message in read_messages(self.database, 'phone', sort='sender_asc')['messages']], [3, 1, 2])
        self.assertEqual([message['id'] for message in read_messages(self.database, 'phone', sort='sender_desc')['messages']], [2, 3, 1])
        self.assertEqual(read_messages(self.database, 'phone', limit=1, offset=1, sort='oldest')['messages'][0]['id'], 2)

    def test_sorting_combines_with_search_and_api(self):
        status, body, _ = self.get('/api/messages?q=alice&sort=oldest&limit=1')
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result['sort'], 'oldest')
        self.assertEqual(result['matched'], 2)
        self.assertEqual(result['messages'][0]['id'], 1)

    def test_viewer_cannot_write_messages(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request('POST', '/api/messages', '{}')
        response = connection.getresponse()
        self.assertEqual(response.status, 501)
        response.read()
        connection.close()
        self.assertEqual(read_messages(self.database, 'phone')['total'], 3)


if __name__ == '__main__':
    unittest.main()
