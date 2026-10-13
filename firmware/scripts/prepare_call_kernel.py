#!/usr/bin/env python3
"""Stage the patched kernel and matching modules on the PC for image assembly."""
import argparse
import json
import os
from pathlib import Path
from scripts.files import file_sha256 as digest
import shutil

from .image_utils import git_revision, kernel_with_original_dtb, run
from .validate_modules import validate as validate_modules

ROOT = Path(__file__).resolve().parents[2]


def prepare(gerda, build, source, wlan, output, objcopy=None):
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output must be empty; preserve previous kernel artifacts')
    pinned = json.loads((ROOT / 'firmware/sources.json').read_text())
    for name, path in [('gerda', gerda), ('kernel', source), ('wlan', wlan.parent)]:
        if git_revision(path) != pinned[name]:
            raise ValueError('Unexpected source revision: ' + name)
    replacement = ROOT / 'firmware/kernel/kaiforward_incall_playback.h'
    installed = source / 'sound/soc/msm/qdsp6v2/kaiforward_incall_playback.h'
    if replacement.read_bytes() != installed.read_bytes():
        raise ValueError('Kernel replacement differs from the current source')
    if b'#include "kaiforward_incall_playback.h"' not in (installed.parent / 'q6voice.c').read_bytes():
        raise ValueError('Kernel replacement is not included')
    if b'kaiforward: incall playback' not in (build / 'vmlinux').read_bytes():
        raise ValueError('Built kernel lacks the playback implementation')
    modules = sorted(p for p in build.rglob('*.ko') if p.name != 'wlan.ko')
    if len({p.name for p in modules}) != len(modules):
        raise ValueError('Duplicate module names')
    if not {'mcDrvModule.ko', 'mcKernelApi.ko'}.issubset(p.name for p in modules):
        raise ValueError('Critical modules missing')
    validation = validate_modules(build, modules + [wlan])
    objcopy = objcopy or os.environ.get('GATEWAY_OBJCOPY') or shutil.which('arm-linux-gnueabi-objcopy') or shutil.which('arm-eabi-objcopy')
    if not objcopy:
        raise ValueError('Provide --objcopy or GATEWAY_OBJCOPY with an ARM objcopy executable')
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    kernel = output / 'zImage-dtb'
    dtb = kernel_with_original_dtb(build / 'arch/arm/boot/zImage', gerda / 'src/boot/8110/zImage', kernel)
    target = output / 'modules'
    (target / 'pronto').mkdir(parents=True)
    for path in modules + [wlan]:
        destination = target / ('pronto/pronto_wlan.ko' if path == wlan else path.name)
        shutil.copyfile(path, destination)
        run([objcopy, '--strip-debug', destination])
        destination.chmod(0o644)
    validate_modules(build, list(target.rglob('*.ko')))
    report = {'schema': 1, 'kernel_payload_sha256': digest(kernel),
              'kernel_release': (build / 'include/config/kernel.release').read_text().strip(),
              'source_revisions': pinned, 'playback_replacement_sha256': digest(replacement),
              'kernel_config_sha256': digest(build / '.config'),
              'modules': {p.relative_to(target).as_posix(): digest(p) for p in sorted(target.rglob('*.ko'))},
              'module_validation': validation, 'dtb': dtb,
              'omitted_upstream_modules': ['core_ctl.ko'],
              'hardware_boot_and_call_audio': 'not tested by this build'}
    (output / 'manifest.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gerda', type=Path, default=ROOT / 'data/gateway-build/gerda')
    p.add_argument('--kernel-build', type=Path, default=ROOT / 'data/gateway-build/kernel-out-gcc48')
    p.add_argument('--kernel-source', type=Path, default=ROOT / 'data/gateway-build/kernel')
    p.add_argument('--wlan', type=Path, default=ROOT / 'data/gateway-build/kernel/drivers/net/wireless/prima/wlan.ko')
    p.add_argument('--output', type=Path, default=ROOT / 'dist/native-call-kernel')
    p.add_argument('--objcopy', type=Path)
    a = p.parse_args()
    print(json.dumps(prepare(a.gerda.resolve(), a.kernel_build.resolve(), a.kernel_source.resolve(),
                             a.wlan.resolve(), a.output.resolve(), a.objcopy), indent=2))
