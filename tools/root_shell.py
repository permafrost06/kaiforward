#!/usr/bin/env python3
"""Run a command through the phone's temporary localhost-only telnet shell."""
import argparse
import re
import secrets
import socket
import shlex
import time


def run(command, port=12323, timeout=60, on_output=None):
    marker = 'KAIFORWARD_' + secrets.token_hex(12)
    output = bytearray()
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        connection.settimeout(0.2)
        deadline = time.monotonic() + timeout
        # Negotiate telnet options before sending shell input.
        greeting = connection.recv(4096)
        for verb, option in re.findall(b'\xff([\xfb-\xfe])(.)', greeting):
            connection.sendall(bytes([255, 254 if verb[0] in (251, 252) else 252, option[0]]))
        connection.sendall(b'stty -echo\r\n')
        time.sleep(0.1)
        try:
            connection.recv(8192)
        except socket.timeout:
            pass
        start = marker + '_START'
        wrapped = "printf '\\n" + start + "\\n'; /system/bin/sh -c " + shlex.quote(command) + "; rc=$?; printf '\\n" + marker + ":%s\\n' \"$rc\"\r\n"
        connection.sendall(wrapped.encode())
        pattern = re.compile(rb'\r?\n' + marker.encode() + rb':(\d+)\r?\n')
        stream_offset = None
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            output.extend(chunk)
            if on_output is not None:
                if stream_offset is None:
                    beginning = re.search(rb'\r?\n' + start.encode() + rb'\r?\n', output)
                    if beginning:
                        stream_offset = beginning.end()
                if stream_offset is not None:
                    while True:
                        newline = output.find(b'\n', stream_offset)
                        if newline < 0:
                            break
                        line = bytes(output[stream_offset:newline]).rstrip(b'\r')
                        stream_offset = newline + 1
                        if not re.fullmatch(marker.encode() + rb':\d+', line):
                            on_output(line.decode(errors='replace') + '\n')
            match = pattern.search(output)
            if match:
                beginning = re.search(rb'\r?\n' + start.encode() + rb'\r?\n', output)
                if not beginning:
                    continue
                text = output[beginning.end():match.start()].decode(errors='replace').replace('\r', '')
                return int(match[1]), text
    raise TimeoutError('Root command did not complete; it may still be running on the phone.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command')
    parser.add_argument('--timeout', type=int, default=60)
    parser.add_argument('--stream', action='store_true', help='Print complete output lines immediately, including before a disconnect.')
    args = parser.parse_args()
    callback = (lambda line: print(line, end='', flush=True)) if args.stream else None
    code, output = run(args.command, timeout=args.timeout, on_output=callback)
    if not args.stream:
        print(output)
    raise SystemExit(code)
