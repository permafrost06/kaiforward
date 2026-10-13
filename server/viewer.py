#!/usr/bin/env python3
"""Read-only browser viewer for the local SMS archive."""
import argparse
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(__file__).resolve().parent / 'viewer-build'
ASSETS = BUILD
CORRESPONDENT = "CASE WHEN json_extract(payload, '$.direction') = 'sent' THEN json_extract(payload, '$.recipient') ELSE json_extract(payload, '$.sender') END"
SORTS = {
    'newest': "CAST(json_extract(payload, '$.timestamp') AS INTEGER) DESC, message_key DESC",
    'oldest': "CAST(json_extract(payload, '$.timestamp') AS INTEGER) ASC, message_key ASC",
    'sender_asc': CORRESPONDENT + " COLLATE NOCASE ASC, CAST(json_extract(payload, '$.timestamp') AS INTEGER) DESC, message_key DESC",
    'sender_desc': CORRESPONDENT + " COLLATE NOCASE DESC, CAST(json_extract(payload, '$.timestamp') AS INTEGER) DESC, message_key DESC",
}


def read_messages(database_path, device_id, query='', limit=50, offset=0, sort='newest', direction='all', number_filter='all'):
    if sort not in SORTS or direction not in ('all', 'received', 'sent') or number_filter not in ('all', 'bd_prefix'):
        raise ValueError('Unknown sort order or filter')
    escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    conditions = 'device_id = ?'
    values = [device_id]
    if direction != 'all':
        conditions += " AND COALESCE(json_extract(payload, '$.direction'), 'received') = ?"
        values.append(direction)
    if number_filter == 'bd_prefix':
        conditions += " AND (" + CORRESPONDENT + " LIKE ? OR " + CORRESPONDENT + " LIKE ?)"
        values.extend(['+880%', '01%'])
    if query:
        conditions += " AND (" + CORRESPONDENT + " LIKE ? ESCAPE char(92) OR json_extract(payload, '$.body') LIKE ? ESCAPE char(92))"
        values.extend(['%' + escaped + '%', '%' + escaped + '%'])
    uri = Path(database_path).resolve().as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as database:
        database.execute('BEGIN')
        total, last_received = database.execute('SELECT COUNT(*), MAX(received_at) FROM messages WHERE device_id=?', (device_id,)).fetchone()
        matched = database.execute('SELECT COUNT(*) FROM messages WHERE ' + conditions, values).fetchone()[0]
        rows = database.execute("SELECT payload FROM messages WHERE " + conditions +
                                " ORDER BY " + SORTS[sort] + " LIMIT ? OFFSET ?",
                                [*values, limit, offset]).fetchall()
    messages = [json.loads(row[0]) for row in rows]
    for message in messages:
        message.setdefault('direction', 'received')
        message.setdefault('recipient', '')
    return {'total': total, 'matched': matched, 'limit': limit, 'offset': offset, 'sort': sort, 'direction': direction, 'number_filter': number_filter,
            'last_received': last_received, 'messages': messages}


def handler_for(database_path, device_id):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self, value):
            try:
                url = urlsplit('http://' + value)
                return url.hostname in ('localhost', '127.0.0.1') and url.port == self.server.server_port and not url.username
            except ValueError:
                return False

        def do_GET(self):
            if not self.valid_host(self.headers.get('Host', '')):
                self.reply(403, {'error': 'Local access required'})
                return
            url = urlsplit(self.path)
            assets = {'/': ('index.html', 'text/html; charset=utf-8')}
            if url.path in assets:
                filename, content_type = assets[url.path]
                self.reply(200, (ASSETS / filename).read_bytes(), content_type)
                return
            if url.path == '/theme.js' or url.path.startswith('/assets/'):
                asset = (ASSETS / url.path.lstrip('/')).resolve()
                if asset.is_relative_to(ASSETS.resolve()) and asset.is_file() and asset.suffix in ('.js', '.css'):
                    content_type = 'text/javascript; charset=utf-8' if asset.suffix == '.js' else 'text/css; charset=utf-8'
                    self.reply(200, asset.read_bytes(), content_type)
                    return
            if url.path != '/api/messages':
                self.reply(404, {'error': 'Not found'})
                return
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + self.headers['Host']:
                self.reply(403, {'error': 'Same-origin access required'})
                return
            if self.headers.get('Sec-Fetch-Site') in ('cross-site', 'same-site'):
                self.reply(403, {'error': 'Same-origin access required'})
                return
            try:
                parameters = parse_qs(url.query)
                limit = int(parameters.get('limit', ['50'])[0])
                offset = int(parameters.get('offset', ['0'])[0])
                query = parameters.get('q', [''])[0].strip()
                sort = parameters.get('sort', ['newest'])[0]
                direction = parameters.get('direction', ['all'])[0]
                number_filter = parameters.get('number_filter', ['all'])[0]
                if not 1 <= limit <= 100 or not 0 <= offset <= 1000000 or len(query) > 512 or sort not in SORTS or direction not in ('all', 'received', 'sent') or number_filter not in ('all', 'bd_prefix'):
                    raise ValueError()
            except ValueError:
                self.reply(400, {'error': 'Invalid search or pagination'})
                return
            try:
                self.reply(200, read_messages(database_path, device_id, query, limit, offset, sort, direction, number_filter))
            except sqlite3.Error:
                self.reply(503, {'error': 'Archive temporarily unavailable'})

    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    args = parser.parse_args()
    if not (BUILD / 'index.html').exists():
        parser.error('Build the viewer first: cd viewer-ui && npm install && npm run build')
    config = json.loads((args.data_dir / 'config.json').read_text())
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(args.data_dir / 'sms.sqlite3', config['device_id']))
    print('SMS viewer: http://127.0.0.1:' + str(args.port), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
