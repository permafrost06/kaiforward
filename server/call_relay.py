#!/usr/bin/env python3
"""Local browser call controls and an authenticated TLS phone audio transport.

Audio is live PCM, never archived. Commands are sent once and timeouts are
reported as uncertain. The phone owns its existing RIL client and audio routes.
"""
import argparse
import asyncio
from contextlib import suppress
import hmac
import json
from pathlib import Path
import signal
import ssl
import struct
import time
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.http11 import Response
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parents[1]
ASSETS = Path(__file__).resolve().parent / 'call-ui'
HELLO, STATE, DOWNLINK, COMMAND, RESULT, START, STOP, UPLINK, PING = range(1, 10)
MAX_FRAME = 8192


async def read_frame(reader):
    length = struct.unpack('>I', await reader.readexactly(4))[0]
    if not 1 <= length <= MAX_FRAME:
        raise ValueError('Invalid phone frame')
    data = await reader.readexactly(length)
    return data[0], data[1:]


def encode_frame(kind, payload=b''):
    if not 1 <= kind <= 9 or len(payload) + 1 > MAX_FRAME:
        raise ValueError('Invalid phone frame')
    return struct.pack('>I', len(payload) + 1) + bytes([kind]) + payload


def validate_command(message):
    slot = message.get('slot')
    action = message.get('action')
    if type(slot) is not int or slot not in (1, 2) or action not in ('answer', 'hangup', 'dial'):
        raise ValueError('Invalid call operation')
    command = f'{slot} {action}'
    if action == 'dial':
        number = message.get('number', '')
        digits = number[1:] if isinstance(number, str) and number.startswith('+') else number
        if not isinstance(digits, str) or not digits or not digits.isascii() or not digits.isdigit() or len(number) > 40:
            raise ValueError('Enter a telephone number')
        command += ' ' + number
    return command.encode('ascii')


class Relay:
    def __init__(self, config, web_port=8768):
        self.config = config
        self.web_port = web_port
        self.phone = None
        self.phone_lock = asyncio.Lock()
        self.state = {'phone_connected': False, 'slots': [], 'media': 'idle'}
        self.browsers = set()
        self.audio_owner = None
        self.audio_slot = 2
        self.pending = {}
        self.command_id = 0
        self.last_state = 0
        self.rx_audio = self.tx_audio = 0

    def public_state(self):
        return {'type': 'state', **self.state, 'audio_attached': self.audio_owner is not None,
                'received_audio_frames': self.rx_audio, 'sent_audio_frames': self.tx_audio}

    async def broadcast(self):
        body = json.dumps(self.public_state())
        for ws in tuple(self.browsers):
            try:
                await asyncio.wait_for(ws.send(body), 1)
            except (ConnectionClosed, TimeoutError):
                self.browsers.discard(ws)

    async def send_phone(self, kind, payload=b''):
        async with self.phone_lock:
            writer = self.phone
            if writer is None:
                raise ConnectionError('Phone is offline')
            if writer.transport.get_write_buffer_size() > 4096 and kind == UPLINK:
                return False  # Drop delayed audio rather than grow latency.
            writer.write(encode_frame(kind, payload))
            await asyncio.wait_for(writer.drain(), 2)
            return True

    async def phone_session(self, reader, writer):
        owns_connection = False
        try:
            kind, payload = await asyncio.wait_for(read_frame(reader), 5)
            hello = json.loads(payload) if kind == HELLO else {}
            if not isinstance(hello, dict) or not hmac.compare_digest(str(hello.get('device_id', '')).encode(), self.config['device_id'].encode()) or not hmac.compare_digest(str(hello.get('token', '')).encode(), self.config['token'].encode()) or hello.get('protocol') != 1:
                raise ValueError('Phone authentication failed')
            if self.phone is not None:
                raise ValueError('Phone is already connected')
            self.phone = writer
            owns_connection = True
            self.state = {'phone_connected': True, 'slots': [], 'media': 'idle'}
            self.last_state = time.monotonic()
            await self.send_phone(HELLO, b'{"ok":true}')
            await self.broadcast()
            while True:
                kind, payload = await asyncio.wait_for(read_frame(reader), 8)
                if kind == STATE:
                    report = json.loads(payload)
                    if not isinstance(report, dict) or not isinstance(report.get('slots'), list) or len(report['slots']) != 2:
                        raise ValueError('Invalid phone state')
                    self.state = {'phone_connected': True, 'slots': report['slots'],
                                  'media': report.get('media', 'idle'), 'audio_error': report.get('audio_error', '')}
                    self.last_state = time.monotonic()
                    # Phone stops its PCM streams if this acknowledgement stops
                    # arriving, even when TCP hasn't noticed a Wi-Fi outage yet.
                    await self.send_phone(PING)
                    if self.audio_owner is not None and self.state['media'] == 'idle':
                        slot = self.audio_slot
                        current = next((s for s in report['slots'] if s.get('slot') == slot), {})
                        if current.get('calls') == 1 and current.get('state') == 0:
                            await self.send_phone(START, bytes([slot]))
                    await self.broadcast()
                elif kind == DOWNLINK:
                    if len(payload) != 320:
                        raise ValueError('Invalid audio frame')
                    self.rx_audio += 1
                    if self.audio_owner is not None:
                        try:
                            await asyncio.wait_for(self.audio_owner.send(payload), .1)
                        except (ConnectionClosed, TimeoutError):
                            await self.detach_audio(self.audio_owner)
                elif kind == RESULT:
                    if len(payload) < 5:
                        raise ValueError('Invalid command result')
                    command_id = struct.unpack('>I', payload[:4])[0]
                    result = json.loads(payload[4:])
                    future = self.pending.get(command_id)
                    if future is not None and not future.done():
                        future.set_result(result)
                elif kind != PING:
                    raise ValueError('Unknown phone frame')
        except (ConnectionError, OSError, ValueError, asyncio.IncompleteReadError, TimeoutError):
            pass  # Never log credentials, call numbers or audio.
        finally:
            if owns_connection and self.phone is writer:
                self.phone = None
                self.state = {'phone_connected': False, 'slots': [], 'media': 'idle'}
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError('Call result unknown; phone disconnected'))
                await self.broadcast()
            writer.close()
            with suppress(OSError, TimeoutError):
                await asyncio.wait_for(writer.wait_closed(), 2)

    async def command(self, message):
        payload = validate_command(message)
        if self.phone is None or time.monotonic() - self.last_state > 5:
            raise ConnectionError('Phone is offline')
        if self.pending:
            raise ValueError('Wait for the current call operation')
        self.command_id = self.command_id % 0xffffffff + 1
        command_id = self.command_id
        future = asyncio.get_running_loop().create_future()
        self.pending[command_id] = future
        try:
            await self.send_phone(COMMAND, struct.pack('>I', command_id) + payload)
            return await asyncio.wait_for(future, 9)
        except TimeoutError:
            return {'ok': False, 'uncertain': True, 'error': 'Call result unknown; check state before retrying'}
        finally:
            self.pending.pop(command_id, None)

    async def detach_audio(self, ws):
        if self.audio_owner is ws:
            self.audio_owner = None
            if self.phone is not None:
                with suppress(ConnectionError, OSError, TimeoutError):
                    await self.send_phone(STOP)

    async def browser(self, ws):
        self.browsers.add(ws)
        commands = set()
        async def execute(message):
            try:
                result = await self.command(message)
                await ws.send(json.dumps({'type': 'result', **result}))
            except (ValueError, ConnectionError, OSError, TimeoutError) as error:
                with suppress(ConnectionClosed):
                    await ws.send(json.dumps({'type': 'result', 'ok': False, 'uncertain': isinstance(error, ConnectionError), 'error': str(error)}))
        try:
            await ws.send(json.dumps(self.public_state()))
            async for data in ws:
                if isinstance(data, bytes):
                    if self.audio_owner is ws and len(data) == 320 and self.state.get('media') == 'running':
                        if await self.send_phone(UPLINK, data):
                            self.tx_audio += 1
                    continue
                try:
                    message = json.loads(data)
                    if not isinstance(message, dict):
                        raise ValueError('Invalid browser message')
                    if message.get('type') == 'audio':
                        slot = message.get('slot')
                        if type(slot) is not int or slot not in (1, 2):
                            raise ValueError('Invalid SIM')
                        if message.get('enabled') is True:
                            if self.audio_owner not in (None, ws):
                                raise ValueError('Another browser is using call audio')
                            self.audio_owner, self.audio_slot = ws, slot
                        else:
                            await self.detach_audio(ws)
                        await self.broadcast()
                    elif message.get('type') == 'command':
                        task = asyncio.create_task(execute(message))
                        commands.add(task)
                        task.add_done_callback(commands.discard)
                    else:
                        raise ValueError('Unknown browser operation')
                except (ValueError, ConnectionError, OSError, TimeoutError) as error:
                    await ws.send(json.dumps({'type': 'result', 'ok': False, 'error': str(error)}))
        except ConnectionClosed:
            pass
        finally:
            self.browsers.discard(ws)
            await self.detach_audio(ws)
            # A sent command still completes once, even if this browser closes.
            # Drain its result; never replay a dial/answer after reconnect.
            if commands:
                await asyncio.gather(*commands, return_exceptions=True)

    def http(self, connection, request):
        host = request.headers.get('Host', '')
        try:
            address = urlsplit('http://' + host)
            allowed = address.hostname in ('127.0.0.1', 'localhost') and address.port == self.web_port and not address.username
        except ValueError:
            allowed = False
        if not allowed:
            return connection.respond(403, 'Local access required\n')
        if request.path == '/call':
            if request.headers.get('Origin') != 'http://' + host:
                return connection.respond(403, 'Same-origin access required\n')
            return None
        routes = {'/': ('index.html', 'text/html; charset=utf-8'),
                  '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                  '/audio-worklet.js': ('audio-worklet.js', 'text/javascript; charset=utf-8'),
                  '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if request.path == '/api/state':
            body, content_type = json.dumps(self.public_state()).encode(), 'application/json'
        elif request.path in routes:
            name, content_type = routes[request.path]
            body = (ASSETS / name).read_bytes()
        else:
            return connection.respond(404, 'Not found\n')
        headers = Headers({'Content-Type': content_type, 'Content-Length': str(len(body)),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'X-Frame-Options': 'DENY', 'Referrer-Policy': 'no-referrer',
                           'Cross-Origin-Resource-Policy': 'same-origin',
                           'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
        return Response(200, 'OK', headers, body)


async def run(args):
    config = json.loads(args.config.read_text())
    relay = Relay(config, args.web_port)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.certificate, args.key)
    phone_server = await asyncio.start_server(relay.phone_session, args.phone_host, args.phone_port,
                                               ssl=context, ssl_handshake_timeout=5)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    async with phone_server, serve(relay.browser, '127.0.0.1', args.web_port,
                                  process_request=relay.http, compression=None,
                                  max_size=8192, max_queue=8, write_limit=4096):
        print(f'Call browser: http://127.0.0.1:{args.web_port}', flush=True)
        await stop.wait()
        if relay.phone is not None:
            with suppress(ConnectionError, OSError, TimeoutError):
                await relay.send_phone(STOP)
            relay.phone.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, default=ROOT / 'data/config.json')
    p.add_argument('--certificate', type=Path, default=ROOT / 'data/native-receiver/certificate.pem')
    p.add_argument('--key', type=Path, default=ROOT / 'data/native-receiver/key.pem')
    p.add_argument('--web-port', type=int, default=8768)
    p.add_argument('--phone-host', default='192.168.0.122')
    p.add_argument('--phone-port', type=int, default=8769)
    asyncio.run(run(p.parse_args()))
