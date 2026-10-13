"""Minimal ADB wrapper bound to one device."""
import contextlib
import subprocess


class AdbError(RuntimeError):
    pass


class Adb:
    def __init__(self, serial=None):
        if serial is None:
            listing = subprocess.run(['adb', 'devices'], check=True, capture_output=True, text=True).stdout
            devices = [line.split()[0] for line in listing.splitlines()[1:] if line.endswith('\tdevice')]
            if len(devices) != 1:
                raise AdbError('Expected exactly one connected device, found {}; pass --serial'.format(len(devices)))
            serial = devices[0]
        self.serial = serial

    def _command(self, *args):
        return ['adb', '-s', self.serial, *args]

    def run(self, *args):
        """Run an adb subcommand for this device, such as forward or reverse."""
        result = subprocess.run(self._command(*args), capture_output=True, text=True)
        if result.returncode:
            raise AdbError('adb {} failed: {}'.format(' '.join(args), result.stderr.strip()))
        return result.stdout

    def shell(self, command, timeout=120):
        """Run a phone command and return its output; fail on a nonzero exit.

        This adbd does not propagate exit codes, so the status is echoed.
        """
        result = subprocess.run(self._command('shell', command + '; echo "<rc=$?>"'),
                                capture_output=True, text=True, timeout=timeout)
        output, _, status = result.stdout.rpartition('<rc=')
        if result.returncode or status.strip() != '0>':
            raise AdbError('Phone command failed ({}): {}\n{}{}'.format(
                status.strip() or result.returncode, command, output, result.stderr))
        return output.replace('\r\n', '\n')

    @contextlib.contextmanager
    def exec_out(self, command):
        """Yield the adb process; read its stdout, then check returncode after exit."""
        process = subprocess.Popen(self._command('exec-out', command), stdout=subprocess.PIPE)
        try:
            yield process
        finally:
            process.stdout.close()
            process.wait()
