#!/usr/bin/env python3
"""Small client for Gecko 48's length-prefixed JSON debugger protocol."""
import json
import socket


class Client:
    def __init__(self, port=16000):
        self.connection = socket.create_connection(('127.0.0.1', port), timeout=10)
        self.connection.settimeout(30)
        self.buffer = bytearray()
        self.greeting = self.receive()
        self.events = []

    def receive(self):
        while b':' not in self.buffer:
            chunk = self.connection.recv(65536)
            if not chunk:
                raise ConnectionError('Debugger connection closed before a packet arrived')
            self.buffer.extend(chunk)
        prefix, _ = self.buffer.split(b':', 1)
        size = int(prefix)
        offset = len(prefix) + 1
        while len(self.buffer) < offset + size:
            chunk = self.connection.recv(65536)
            if not chunk:
                raise ConnectionError('Debugger connection closed')
            self.buffer.extend(chunk)
        packet = bytes(self.buffer[offset:offset + size])
        del self.buffer[:offset + size]
        return json.loads(packet)

    def request(self, actor, request_type, **fields):
        packet = json.dumps({'to': actor, 'type': request_type, **fields}, ensure_ascii=True).encode()
        self.connection.sendall(str(len(packet)).encode() + b':' + packet)
        while True:
            response = self.receive()
            if response.get('from') == actor:
                return response
            self.events.append(response)

    def close(self):
        self.connection.close()


if __name__ == '__main__':
    client = Client()
    print(json.dumps(client.request('root', 'listTabs'), indent=2))
    client.close()
