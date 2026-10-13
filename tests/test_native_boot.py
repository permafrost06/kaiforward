"""Regression coverage for the native first-boot recovery loop."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / 'firmware/scripts'
sys.path.insert(0, str(SCRIPTS))
from build_gateway_image import normalize_modes
from package_native_boot_repair import repair_cpio, DIRECTORIES
from validate_images import cpio_files


def archive(entries):
    raw = bytearray()
    for name, mode, content in entries + [('TRAILER!!!', 0, b'')]:
        name = name.encode() + b'\0'
        fields = [0, mode, 0, 0, 1, 0, len(content), 0, 0, 0, 0, len(name), 0]
        raw += b'070701' + ''.join(f'{v:08x}' for v in fields).encode() + name
        raw += b'\0' * (-len(raw) % 4)
        raw += content
        raw += b'\0' * (-len(raw) % 4)
    return bytes(raw)


class NativeBootTests(unittest.TestCase):
    def test_private_host_staging_root_is_traversable_on_phone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            (root / 'data').mkdir(mode=0o700)
            (root / 'init').write_bytes(b'init')
            (root / 'init').chmod(0o700)
            normalize_modes(root)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / 'data').stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / 'init').stat().st_mode & 0o777, 0o755)

    def test_repair_preserves_payloads_and_replaces_bad_modes_and_startup(self):
        entries = [(name, 0o40700, b'') for name in sorted(DIRECTORIES)]
        entries += [('init.kaiforward-native.rc', 0o100600, b'service kaiforward-upload old\n'),
                    ('init', 0o100755, b'original init'), ('keep.rc', 0o100644, b'unchanged')]
        startup = (ROOT / 'firmware/native/init.native.rc').read_bytes()
        repaired = cpio_files(repair_cpio(archive(entries), startup))
        for name in DIRECTORIES:
            self.assertEqual(repaired[name][0], 0o40755)
        self.assertEqual(repaired['init'], (0o100755, b'original init'))
        self.assertEqual(repaired['keep.rc'], (0o100644, b'unchanged'))
        self.assertEqual(repaired['init.kaiforward-native.rc'], (0o100644, startup))
        for line in startup.decode().splitlines():
            if line.startswith('service '):
                self.assertLessEqual(len(line.split()[1]), 16)
        self.assertIn(b'service kf-upload ', startup)

    def test_repair_refuses_missing_root_entries_and_truncated_archive(self):
        with self.assertRaises(ValueError):
            repair_cpio(archive([('init.kaiforward-native.rc', 0o100644, b'old')]), b'new')
        with self.assertRaises(ValueError):
            repair_cpio(b'', b'new')


if __name__ == '__main__':
    unittest.main()
