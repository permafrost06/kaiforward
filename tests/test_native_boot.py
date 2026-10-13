"""Regression coverage for the native first-boot recovery loop."""
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
from firmware.scripts.image_utils import normalize_modes
from firmware.scripts.build_native_image import native_startup, DISABLED


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

    def test_native_startup_disables_competing_services_preserving_options(self):
        original = ''.join(f'service {name} /system/bin/{name}\n    class main\n' for name in DISABLED)
        result = native_startup(original)
        self.assertTrue(result.startswith('import /init.kaiforward-native.rc\n'))
        for name in DISABLED:
            self.assertIn(f'service {name} /system/bin/{name}\n    disabled\n    class main\n', result)
        startup = (ROOT / 'firmware/native/init.native.rc').read_text()
        names = [line.split()[1] for line in startup.splitlines() if line.startswith('service ')]
        self.assertTrue(all(len(name) <= 16 for name in names))
        self.assertIn('kf-upload', names)
        self.assertIn('kf-permission', names)
        self.assertIn('kf-relay', names)

    def test_unexpected_upstream_services_are_rejected(self):
        with self.assertRaises(ValueError):
            native_startup('service b2g /system/b2g/b2g\n')
        original = ''.join(f'service {name} /bin/{name}\n' for name in DISABLED)
        with self.assertRaises(ValueError):
            native_startup(original + 'service b2g /bin/duplicate\n')


if __name__ == '__main__':
    unittest.main()
