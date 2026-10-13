"""Start the temporary root shell on a stock Nokia 8110 4G.

Serves the one-button page in access.html to the phone over USB. Open
http://127.0.0.1:8767 in the phone's browser and press the button; the page
asks the firmware's command API to start busybox telnetd on the phone's
loopback interface. Rebooting the phone stops it. The page must be served:
the phone's browser does not open file:// addresses.
"""
import argparse
from pathlib import Path
import http.server
import sys
import threading
import time

from scripts.adb import Adb
from scripts import root_shell

PHONE_PAGE_PORT = 8767
TIMEOUT = 600

PAGE = Path(__file__).with_name('access.html')


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/':
            self.send_error(404)
            return
        body = PAGE.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def wait_for_shell(adb):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if root_shell.available(adb):
            print('Root shell is running.')
            return 0
        time.sleep(2)
    print('Timed out waiting for the root shell.', file=sys.stderr)
    return 1


def serve(adb):
    """Serve the page to the phone at http://127.0.0.1:8767 until the shell starts."""
    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        page = 'tcp:{}'.format(PHONE_PAGE_PORT)
        adb.run('reverse', page, 'tcp:{}'.format(server.server_address[1]))
        try:
            print('On the phone, open http://127.0.0.1:{} in the browser and press '
                  '"Enable temporary access".'.format(PHONE_PAGE_PORT), flush=True)
            return wait_for_shell(adb)
        finally:
            adb.run('reverse', '--remove', page)
            server.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--serial', help='ADB serial (default: the only connected device)')
    args = parser.parse_args(argv)
    adb = Adb(args.serial)
    if root_shell.available(adb):
        print('The root shell is already running.')
        return 0
    return serve(adb)

if __name__ == '__main__':
    sys.exit(main())
