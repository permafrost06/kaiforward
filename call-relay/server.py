#!/usr/bin/env python3
"""Browser call controls for a KaiForward phone, plus its authenticated TLS audio transport.

The phone connects to --phone-host:--phone-port over TLS. The browser page is
plain HTTP on --web-host:--web-port (loopback by default). Audio is live PCM and
never stored. Commands are sent once; timeouts are reported as uncertain.
"""
import argparse
import asyncio
from contextlib import suppress
import hmac
import json
from pathlib import Path
import re
import signal
import ssl
import struct
import time
from urllib.parse import urlsplit

from websockets.asyncio.server import serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

ASSETS = Path(__file__).resolve().parent
HELLO, STATE, DOWNLINK, COMMAND, RESULT, START, STOP, UPLINK, PING = range(1, 10)
MAX_FRAME = 8192
USSD_CODE = re.compile(r'[0-9*#+]{1,40}')
USSD_STATES = ('idle', 'waiting', 'reply', 'done', 'failed', 'cancelled')
SMS_STATES = ('queued', 'submitting', 'sent', 'failed', 'uncertain')
# One segment, as the phone does not split texts: 160 septets of the GSM
# default alphabet (escaped characters take two), otherwise 70 UTF-16 units.
GSM_BASIC = ('@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !"#¤%&\'()*+,-./0123456789:;<=>?'
             '¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà')
GSM_ESCAPED = '^{}\\[~]|€'
SMS_SEPTETS, SMS_UNITS = 160, 70
PAGES = {'/': ('index.html', 'text/html; charset=utf-8'),
         '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
         '/audio-worklet.js': ('audio-worklet.js', 'text/javascript; charset=utf-8')}


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


def valid_number(number, shortest=1, longest=40):
    digits = number[1:] if isinstance(number, str) and number.startswith('+') else number
    return isinstance(digits, str) and digits.isascii() and digits.isdigit() and \
        shortest <= len(digits) <= longest and len(number) <= 40


def valid_text(text):
    if not isinstance(text, str) or not text or any(c < ' ' and c not in '\n\r\t' for c in text):
        return False
    if all(c in GSM_BASIC or c in GSM_ESCAPED for c in text):
        return len(text) + sum(c in GSM_ESCAPED for c in text) <= SMS_SEPTETS
    try:
        return len(text.encode('utf-16-le')) // 2 <= SMS_UNITS
    except UnicodeEncodeError:  # A lone surrogate.
        return False


def validate_command(message):
    slot = message.get('slot')
    action = message.get('action')
    if type(slot) is not int or slot not in (1, 2) or action not in ('answer', 'hangup', 'dial', 'ussd', 'ussd-cancel', 'sms'):
        raise ValueError('Invalid call operation')
    command = f'{slot} {action}'
    if action == 'sms':
        number, text = message.get('number', ''), message.get('text', '')
        if not valid_number(number, 3, 20):
            raise ValueError('Enter a telephone number of 3 to 20 digits')
        if not valid_text(text):
            raise ValueError(f'Enter a message of 1 to {SMS_SEPTETS} characters, or {SMS_UNITS} with emoji or non-Latin text')
        return f'{command} {number} {text}'.encode()
    if action == 'ussd':
        code = message.get('code', '')
        if not isinstance(code, str) or not USSD_CODE.fullmatch(code):
            raise ValueError('Enter a USSD code using digits, * and #')
        command += ' ' + code
    if action == 'dial':
        number = message.get('number', '')
        if not valid_number(number):
            raise ValueError('Enter a telephone number')
        command += ' ' + number
    return command.encode('ascii')


def validate_ussd(report):
    """Per-SIM USSD state from the phone; [] when its helper predates USSD."""
    ussd = report.get('ussd', [])
    if not isinstance(ussd, list) or len(ussd) not in (0, 2):
        raise ValueError('Invalid USSD state')
    for slot, entry in enumerate(ussd, 1):
        if entry is not None and not (
                isinstance(entry, dict) and entry.get('slot') == slot and
                type(entry.get('id')) is int and entry.get('state') in USSD_STATES and
                type(entry.get('type')) is int and type(entry.get('error')) is int and
                isinstance(entry.get('message'), str)):
            raise ValueError('Invalid USSD state')
    return [entry and {k: entry[k] for k in ('slot', 'id', 'state', 'type', 'error', 'message')}
            for entry in ussd]


def validate_sms(report):
    """Recent outbox jobs per SIM; [] when the phone's helper predates SMS sending."""
    sms = report.get('sms', [])
    if not isinstance(sms, list) or len(sms) not in (0, 2):
        raise ValueError('Invalid SMS state')
    for slot, entry in enumerate(sms, 1):
        if entry is not None and not (
                isinstance(entry, dict) and entry.get('slot') == slot and
                isinstance(entry.get('jobs'), list) and len(entry['jobs']) <= 5 and
                all(isinstance(job, dict) and type(job.get('id')) is int and
                    job.get('status') in SMS_STATES for job in entry['jobs'])):
            raise ValueError('Invalid SMS state')
    return [entry and {'slot': entry['slot'], 'jobs': [{'id': job['id'], 'status': job['status']}
                                                       for job in entry['jobs']]}
            for entry in sms]


class Relay:
    def __init__(self, token, web_hosts=('127.0.0.1', 'localhost')):
        self.token = token
        self.web_hosts = set(web_hosts)
        self.phone = None
        self.phone_lock = asyncio.Lock()
        self.state = {'phone_connected': False, 'slots': [], 'media': 'idle'}
        self.browsers = set()
        self.audio_owner = None
        self.audio_slot = 2
        self.pending = {}
        self.command_id = 0
        self.last_state = 0

    def public_state(self):
        return {'type': 'state', **self.state, 'audio_attached': self.audio_owner is not None}

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
            if not isinstance(hello, dict) or hello.get('protocol') != 1 or \
                    not hmac.compare_digest(str(hello.get('token', '')).encode(), self.token.encode()):
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
                                  'media': report.get('media', 'idle'), 'audio_error': report.get('audio_error', ''),
                                  'ussd': validate_ussd(report), 'sms': validate_sms(report)}
                    self.last_state = time.monotonic()
                    # The phone stops its PCM streams if this acknowledgement stops
                    # arriving, even when TCP hasn't noticed a network outage yet.
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
                    if self.audio_owner is not None:
                        try:
                            await asyncio.wait_for(self.audio_owner.send(payload), .1)
                        except (ConnectionClosed, TimeoutError):
                            await self.detach_audio(self.audio_owner)
                elif kind == RESULT:
                    if len(payload) < 5:
                        raise ValueError('Invalid command result')
                    command_id = struct.unpack('>I', payload[:4])[0]
                    future = self.pending.get(command_id)
                    if future is not None and not future.done():
                        future.set_result(json.loads(payload[4:]))
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
                        await self.send_phone(UPLINK, data)
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
            allowed = address.hostname in self.web_hosts and not address.username
        except ValueError:
            allowed = False
        # A host allowlist stops DNS rebinding; the origin check stops cross-site sockets.
        if not allowed:
            return connection.respond(403, 'Unknown host\n')
        if request.path == '/call':
            if request.headers.get('Origin') not in ('http://' + host, 'https://' + host):
                return connection.respond(403, 'Same-origin access required\n')
            return None
        if request.path not in PAGES:
            return connection.respond(404, 'Not found\n')
        name, content_type = PAGES[request.path]
        body = (ASSETS / name).read_bytes()
        headers = Headers({'Content-Type': content_type, 'Content-Length': str(len(body)),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                           'Referrer-Policy': 'no-referrer',
                           'Content-Security-Policy': "default-src 'none'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"})
        return Response(200, 'OK', headers, body)


async def run(args):
    token = args.token_file.read_text().strip()
    if not token:
        raise SystemExit('Token file is empty')
    relay = Relay(token, ['127.0.0.1', 'localhost', *args.allow_host])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.certificate, args.key)
    phone_server = await asyncio.start_server(relay.phone_session, args.phone_host, args.phone_port,
                                               ssl=context, ssl_handshake_timeout=5)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    async with phone_server, serve(relay.browser, args.web_host, args.web_port,
                                  process_request=relay.http, compression=None,
                                  max_size=8192, max_queue=8, write_limit=4096):
        print(f'Phone: tls://{args.phone_host}:{args.phone_port}  Browser: http://{args.web_host}:{args.web_port}', flush=True)
        await stop.wait()
        phone = relay.phone
        if phone is not None:
            with suppress(ConnectionError, OSError, TimeoutError):
                await relay.send_phone(STOP)
            phone.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--token-file', type=Path, required=True, help='File containing the phone receiver token')
    p.add_argument('--certificate', type=Path, required=True, help='TLS certificate chain presented to the phone')
    p.add_argument('--key', type=Path, required=True, help='TLS private key')
    p.add_argument('--phone-host', default='0.0.0.0', help='Phone listener address, e.g. this machine\'s Tailscale IP')
    p.add_argument('--phone-port', type=int, default=8769)
    p.add_argument('--web-host', default='127.0.0.1')
    p.add_argument('--web-port', type=int, default=8768)
    p.add_argument('--allow-host', action='append', default=[],
                   help='Extra browser Host name, e.g. the MagicDNS name used by tailscale serve')
    asyncio.run(run(p.parse_args()))
