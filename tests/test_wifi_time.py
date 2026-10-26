"""Exercise Wi-Fi time sync with fake Android commands, without setting host time."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

NATIVE = Path(__file__).resolve().parents[1] / 'firmware/native'


class WifiTimeTests(unittest.TestCase):
    def test_ntp_callbacks_retry_upload_without_running_dhcp(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (NATIVE / 'udhcpc.sh').read_text().replace('/data/kaiforward', str(root))
            script = script.replace('PATH=/system/bin:/system/xbin', 'PATH=/usr/bin:/bin')
            hook = root / 'hook.sh'
            hook.write_text(script)
            for event in ('step', 'stratum'):
                subprocess.run(['sh', str(hook), event], check=True,
                               env={**os.environ, 'interface': ''})
                self.assertTrue((root / 'sync').exists())
                (root / 'sync').unlink()
            subprocess.run(['sh', str(hook), 'periodic'], check=True)
            self.assertFalse((root / 'sync').exists())
            self.assertNotEqual(subprocess.run(['sh', str(hook), 'bound'],
                                env={**os.environ, 'interface': ''}).returncode, 0)

    def test_ntp_waits_for_route_restarts_and_stops_with_wifi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            commands = root / 'bin'
            commands.mkdir()
            carrier = root / 'carrier'
            carrier.write_text('1\n')
            route = root / 'route'
            log = root / 'ntp-starts'
            command = commands / 'command'
            command.write_text('''#!/bin/sh
case "${0##*/}" in
    busybox)
        case "$1" in
            awk|grep) applet=$1; shift; exec "$applet" "$@" ;;
            ntpd) echo "$$ $*" >> "$NTP_STARTS"; exec /bin/sleep 60 ;;
            udhcpc) exec /bin/sleep 60 ;;
        esac ;;
    wpa_supplicant) exec /bin/sleep 60 ;;
    ip) [ ! -f "$NTP_ROUTE" ] || echo 'default via 192.0.2.1'; exit 0 ;;
    sleep) exec /bin/sleep 0.05 ;;
    kaiforward-native) [ "$1" != --wifi-config ] || echo config ;;
esac
exit 0
''')
            command.chmod(0o755)
            for name in ('busybox', 'wpa_supplicant', 'ip', 'sleep', 'kaiforward-native',
                         'ndc', 'ifconfig', 'chown', 'chmod'):
                (commands / name).symlink_to(command)
            script = (NATIVE / 'wifi.sh').read_text()
            script = script.replace('PATH=/system/bin:/system/xbin', f'PATH={commands}:/usr/bin:/bin')
            script = script.replace('/data/kaiforward', str(root / 'home'))
            script = script.replace('/data/misc/wifi', str(root / 'wifi'))
            script = script.replace('/sys/class/net/wlan0/carrier', str(carrier))
            (root / 'home').mkdir()
            supervisor = root / 'wifi.sh'
            supervisor.write_text(script)
            process = subprocess.Popen(['sh', str(supervisor)],
                env={**os.environ, 'NTP_STARTS': str(log), 'NTP_ROUTE': str(route)})

            def wait_for(predicate):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if predicate():
                        return
                    time.sleep(.02)
                self.fail('Wi-Fi time service did not reach expected state')

            def starts():
                return log.read_text().splitlines() if log.exists() else []

            try:
                time.sleep(.2)
                self.assertEqual(starts(), [])
                route.touch()
                wait_for(lambda: len(starts()) == 1)
                pid = int(starts()[0].split()[0])
                self.assertIn('-n -p 162.159.200.1 -p 162.159.200.123 -S', starts()[0])
                os.kill(pid, 15)
                wait_for(lambda: len(starts()) == 2)
                pid = int(starts()[-1].split()[0])
                route.unlink()
                wait_for(lambda: not Path(f'/proc/{pid}').exists())
                route.touch()
                wait_for(lambda: len(starts()) == 3)
                pid = int(starts()[-1].split()[0])
            finally:
                process.terminate()
                process.wait(timeout=5)
            self.assertFalse(Path(f'/proc/{pid}').exists())
