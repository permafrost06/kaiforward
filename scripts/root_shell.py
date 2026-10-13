"""Root commands on stock KaiOS through the temporary loopback telnet shell.

`python3 -m scripts.access` starts the shell on the phone. Its telnetd
listens on phone port 2323; ADB forwards PC port 12323 to it.
"""
import contextlib
import re
import secrets
import shlex
import socket
import threading
import time

from scripts.adb import AdbError

PHONE_PORT = 2323
PC_PORT = 12323
IAC, DONT, DO, WONT, WILL = 255, 254, 253, 252, 251


def telnet_run(command, port=PC_PORT, timeout=120):
    """Run a command in a fresh telnet session; return (exit code, output)."""
    marker = 'KF' + secrets.token_hex(8)
    output = bytearray()
    with socket.create_connection(('127.0.0.1', port), timeout=5) as connection:
        connection.settimeout(0.5)
        try:
            greeting = connection.recv(4096)
        except socket.timeout:
            greeting = b''
        # Refuse every option the server offers so the session stays a plain stream.
        for verb, option in re.findall(rb'\xff([\xfb-\xfe])(.)', greeting, re.S):
            reply = DONT if verb[0] in (WILL, WONT) else WONT
            connection.sendall(bytes([IAC, reply, option[0]]))
        # The echoed input shows the markers after a literal backslash-n, so only printf's
        # real newlines match below.
        script = ("stty -echo; printf '\\n{m}_BEGIN\\n'; /system/bin/sh -c {command}; "
                  "printf '\\n{m}_END:%s\\n' $?\r\n").format(m=marker, command=shlex.quote(command))
        connection.sendall(script.encode())
        end = re.compile(rb'\r?\n' + marker.encode() + rb'_END:(\d+)\r?\n')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                if not output:
                    raise AdbError('The root shell is not running; start it with python3 -m scripts.access')
                break
            output.extend(chunk)
            match = end.search(output)
            if match:
                begin = re.search(rb'\r?\n' + marker.encode() + rb'_BEGIN\r?\n', output)
                text = output[begin.end():match.start()].decode(errors='replace')
                return int(match[1]), text.replace('\r\n', '\n')
    raise AdbError('Root shell command did not finish: ' + command)


def available(adb):
    """Forward the telnet port and report whether the root shell answers."""
    adb.run('forward', 'tcp:{}'.format(PC_PORT), 'tcp:{}'.format(PHONE_PORT))
    try:
        code, output = telnet_run('busybox id -u', timeout=10)
    except (OSError, AdbError):
        return False
    return code == 0 and output.strip() == '0'


class RootShell:
    """Same interface as Adb, but every phone command runs as root via telnet."""

    def __init__(self, adb):
        self.adb = adb
        self.serial = adb.serial

    def shell(self, command, timeout=120):
        code, output = telnet_run(command, timeout=timeout)
        if code:
            raise AdbError('Phone command failed ({}): {}\n{}'.format(code, command, output))
        return output

    @contextlib.contextmanager
    def exec_out(self, command, timeout=6 * 3600):
        """Stream the command's stdout over a reverse-tunnelled loopback socket.

        The PC listens; the phone connects out with nc, so the phone opens no
        listening port of its own.
        """
        with socket.create_server(('127.0.0.1', 0)) as server:
            port = server.getsockname()[1]
            self.adb.run('reverse', 'tcp:{}'.format(port), 'tcp:{}'.format(port))
            result = {}

            def run():
                try:
                    result['code'], result['output'] = telnet_run(
                        '{{ {}; }} | busybox nc 127.0.0.1 {}'.format(command, port), timeout=timeout)
                except Exception as error:  # reported through returncode below
                    result['error'] = error

            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            server.settimeout(30)
            try:
                connection, _ = server.accept()
            except socket.timeout:
                worker.join(5)
                raise AdbError('Phone did not connect for streaming: {}'.format(result))
            process = Stream(connection)
            try:
                yield process
            finally:
                process.stdout.close()
                connection.close()
                worker.join(60)
                self.adb.run('reverse', '--remove', 'tcp:{}'.format(port))
                process.returncode = 0 if result.get('code') == 0 else result.get('code', 1)


class Stream:
    def __init__(self, connection):
        self.stdout = connection.makefile('rb')
        self.returncode = None
