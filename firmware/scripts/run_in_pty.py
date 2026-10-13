#!/usr/bin/env python3
"""Give an interactive legacy compiler VM a PTY, including in a build service."""
import errno
import os
import pty
import sys


def main():
    if len(sys.argv) < 2:
        raise SystemExit('Usage: run_in_pty.py COMMAND [ARG ...]')
    pid, descriptor = pty.fork()
    if pid == 0:
        os.execvp(sys.argv[1], sys.argv[1:])
    try:
        while True:
            try:
                chunk = os.read(descriptor, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
    finally:
        os.close(descriptor)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status)


if __name__ == '__main__':
    sys.exit(main())
