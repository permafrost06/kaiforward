#!/usr/bin/env python3
"""PoC receiver fixture for native upload contract tests; no service is started on import."""
import argparse
from contextlib import closing
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import ssl
import time

ROOT = Path(__file__).resolve().parents[1]
MAX_BODY = 1024 * 1024


def load_config(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'config.json'
    if not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump({'token': secrets.token_urlsafe(32), 'device_id': 'nokia-8110'}, stream)
    return json.loads(path.read_text())


def connect(path):
    database = sqlite3.connect(path, timeout=15)
    database.execute('PRAGMA journal_mode=WAL')
    database.execute('''CREATE TABLE IF NOT EXISTS messages (
        device_id TEXT NOT NULL, message_key TEXT NOT NULL, payload TEXT NOT NULL,
        received_at INTEGER NOT NULL, PRIMARY KEY (device_id, message_key))''')
    return database


def validate(payload, device_id):
    if not isinstance(payload, dict) or payload.get('device_id') != device_id:
        raise ValueError('Unknown device')
    messages = payload.get('messages')
    if not isinstance(messages, list) or not 1 <= len(messages) <= 50:
        raise ValueError('Expected 1 to 50 messages')
    normalized = []
    keys = set()
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError('Invalid message')
        ident, stamp = message.get('id'), message.get('timestamp')
        if type(ident) is not int or ident < 0 or type(stamp) is not int or not 0 <= stamp < 10**14:
            raise ValueError('Invalid id or timestamp')
        key = str(ident) + ':' + str(stamp)
        sender, body = message.get('sender'), message.get('body')
        direction, recipient = message.get('direction', 'received'), message.get('recipient', '')
        if message.get('key') != key or key in keys:
            raise ValueError('Invalid or repeated message key')
        if not isinstance(sender, str) or len(sender) > 128 or not isinstance(body, str) or len(body) > 65536:
            raise ValueError('Invalid sender or body')
        if direction not in ('received', 'sent') or not isinstance(recipient, str) or len(recipient) > 128:
            raise ValueError('Invalid direction or recipient')
        if direction == 'sent' and not recipient:
            raise ValueError('Sent SMS requires a recipient')
        keys.add(key)
        record = {'key': key, 'id': ident, 'timestamp': stamp, 'sender': sender, 'body': body,
                  'direction': direction, 'recipient': recipient}
        if 'sim_slot' in message:
            if type(message['sim_slot']) is not int or message['sim_slot'] not in (1, 2):
                raise ValueError('Invalid SIM slot')
            record['sim_slot'] = message['sim_slot']
        normalized.append(record)
    return normalized


def handler_for(database_path, config):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # Request bodies, phone numbers and credentials never enter logs.
            pass

        def respond(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            expected = ('Bearer ' + config['token']).encode('utf-8')
            actual = self.headers.get('Authorization', '').encode('utf-8')
            if not hmac.compare_digest(expected, actual):
                self.respond(401, {'error': 'Unauthorized'})
                return False
            return True

        def do_GET(self):
            if self.path == '/health':
                self.respond(200, {'ok': True})
                return
            if self.path != '/v1/stats':
                self.respond(404, {'error': 'Not found'})
                return
            if not self.authorized():
                return
            with closing(connect(database_path)) as database:
                count = database.execute('SELECT COUNT(*) FROM messages WHERE device_id=?', (config['device_id'],)).fetchone()[0]
            self.respond(200, {'messages': count})

        def do_POST(self):
            if self.path != '/v1/messages':
                self.respond(404, {'error': 'Not found'})
                return
            if not self.authorized():
                return
            self.connection.settimeout(15)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_BODY:
                    self.respond(413, {'error': 'Body size outside limits'})
                    return
                messages = validate(json.loads(self.rfile.read(length)), config['device_id'])
            except (ValueError, TypeError, TimeoutError):
                self.respond(400, {'error': 'Invalid request'})
                return
            try:
                with closing(connect(database_path)) as database, database:
                    for message in messages:
                        serialized = json.dumps(message, sort_keys=True, ensure_ascii=False)
                        previous = database.execute('SELECT payload FROM messages WHERE device_id=? AND message_key=?',
                                                    (config['device_id'], message['key'])).fetchone()
                        if previous:
                            # Compare defaults too, so archives from the received-only app remain replayable.
                            prior = validate({'device_id': config['device_id'], 'messages': [json.loads(previous[0])]}, config['device_id'])[0]
                            if prior != message:
                                raise ValueError('Message key already has different content')
                        database.execute('INSERT OR IGNORE INTO messages VALUES (?, ?, ?, ?)',
                                         (config['device_id'], message['key'], serialized, int(time.time() * 1000)))
                self.respond(200, {'accepted': [message['key'] for message in messages]})
            except ValueError:
                self.respond(409, {'error': 'Message key conflict'})
            except sqlite3.Error:
                self.respond(503, {'error': 'Storage unavailable'})
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1', help='Bind address; set a LAN address for Wi-Fi forwarding')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--tls-cert', type=Path)
    parser.add_argument('--tls-key', type=Path)
    args = parser.parse_args()
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error('--tls-cert and --tls-key must be provided together')
    os.umask(0o077)
    config = load_config(args.data_dir)
    database_path = args.data_dir / 'sms.sqlite3'
    connect(database_path).close()
    server = ThreadingHTTPServer((args.host, args.port), handler_for(database_path, config))
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    scheme = 'https' if args.tls_cert else 'http'
    print('KaiForward receiver listening on ' + scheme + '://' + args.host + ':' + str(args.port), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
