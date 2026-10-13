"""Exercise restore safety failures with a fake tool; never access real blocks."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'firmware/scripts'))
spec = importlib.util.spec_from_file_location('restore', ROOT / 'firmware/scripts/package_stock_sd_restore.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)
HASHES = {'system': '054d3a50cd160435402a5229165219fab4e043880d0ff5b22c8d9e939fc4c9a5',
          'boot': '4f68161404d3fb3d2172d422f792aef4b9daef247aa02d51bb3dd25702e9fd3d'}
FAKE = r'''
import json, os, pathlib, sys
a=sys.argv[1:]
command=a[0]
mode=os.environ['RESTORE_TEST_FAILURE']
sizes={'system':838860800,'boot':26165248}
hashes=HASHES
name=pathlib.Path(a[-1]).name.split('.')[0]
if command=='awk':
    pattern=a[-2]
    if a[1]=='-v':
        name=a[2].split('=')[1].split('.')[0]
        print(hashes[name])
    elif 'mmcblk1p1' in pattern:
        sys.exit(1 if mode=='wrong-card' else 0)
    elif '$2 == "/system"' in pattern:
        sys.exit(0 if mode=='mounted' else 1)
    elif 'print $1' in pattern and mode=='mounted-alias':
        print('/dev/block/mounted-alias')
elif command=='sha256sum':
    if a[1]=='-c':
        sys.exit(1 if mode=='bad-image-hash' else 0)
    print(('bad' if mode==name+'-bad-readback' else hashes[name])+'  '+a[-1])
elif command=='stat':
    print(sizes[name])
elif command=='blockdev':
    print(sizes[name]-(1 if mode=='boot-size-mismatch' and name=='boot' else 0))
elif command=='readlink':
    print('/dev/mock/system' if name=='mounted-alias' else '/dev/mock/'+name)
elif command=='test':
    sys.exit(1 if mode=='not-block' else 0)
elif command=='dd':
    name=next(x for x in a if x.startswith('of=')).rsplit('/',1)[1]
    with open(os.environ['RESTORE_TEST_WRITES'],'a') as f: f.write(name+'\n')
    sys.exit(1 if mode==name+'-write-failure' else 0)
elif command=='date':
    print('20261013-000000')
elif command=='sync':
    pass
else:
    raise RuntimeError(a)
'''


class RestoreSafety(unittest.TestCase):
    def run_case(self, failure):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / 'fake-busybox'
            tool.write_text('#!' + sys.executable + '\n' + FAKE.replace('HASHES', repr(HASHES)))
            tool.chmod(0o755)
            directory = root / 'images'
            directory.mkdir()
            script = restore.SHELL.replace('BB=/tmp/busybox', 'BB=' + str(tool))
            script = script.replace('DIRECTORY=/sdcard/kaiforward-stock-restore', 'DIRECTORY=' + str(directory))
            writes = root / 'writes'
            env = dict(os.environ, RESTORE_TEST_FAILURE=failure, RESTORE_TEST_WRITES=str(writes))
            result = subprocess.run(['sh'], input=script, text=True, env=env, capture_output=True)
            return result.returncode, writes.read_text().splitlines() if writes.exists() else []

    def test_guards_prevent_all_writes(self):
        for failure in ['wrong-card', 'bad-image-hash', 'boot-size-mismatch', 'mounted', 'mounted-alias', 'not-block']:
            with self.subTest(failure=failure):
                code, writes = self.run_case(failure)
                self.assertNotEqual(code, 0)
                self.assertEqual(writes, [])

    def test_system_failure_prevents_boot_write(self):
        for failure in ['system-write-failure', 'system-bad-readback']:
            with self.subTest(failure=failure):
                code, writes = self.run_case(failure)
                self.assertNotEqual(code, 0)
                self.assertEqual(writes, ['system'])

    def test_boot_failure_is_not_reported_as_success(self):
        for failure in ['boot-write-failure', 'boot-bad-readback']:
            with self.subTest(failure=failure):
                code, writes = self.run_case(failure)
                self.assertNotEqual(code, 0)
                self.assertEqual(writes, ['system', 'boot'])

    def test_success_restores_system_then_boot(self):
        self.assertEqual(self.run_case('none'), (0, ['system', 'boot']))


if __name__ == '__main__':
    unittest.main()
