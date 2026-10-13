# Recovery package inputs

`scripts/flasher.py` builds its recovery packages from these files. They are
redistributed unmodified. They are separate programs and data, not part of
KaiForward's own code, and each keeps its own license.

| File | SHA-256 |
|---|---|
| `update-binary` | `492c4408b564bdd63e3861289da9c00e1aad82ccfa827c7209af15b22390e2b4` |
| `busybox` | `eab7d79401911369d90960e44d4148c6b8cb082239e8c63b1beafcf8a02c1fb1` |
| `testkey.x509.pem` | `a4384ba815b9499a5ce349b4e33c1755278873fe2eac150a068823f526e6dbde` |
| `testkey.pk8` | `495675d32e89a149d5abe191f4e9c0e218b9068714e9b53a7c91e164a0741a23` |

## busybox

BusyBox 1.20.0, statically linked ARM binary. It is byte-identical to the
official prebuilt `busybox-armv6l` published by the BusyBox project:
https://busybox.net/downloads/binaries/1.20.0/busybox-armv6l

BusyBox is licensed under the GNU General Public License version 2 only; see
`LICENSE.busybox`. The binary is statically linked with uClibc 0.9.33.2,
licensed under the GNU Lesser General Public License version 2.1; see
`LICENSE.uClibc`.

Corresponding source:

- BusyBox 1.20.0: https://busybox.net/downloads/busybox-1.20.0.tar.bz2
  (SHA-256 `3c56508d984db1178664241ad548d118fb0aa71cdd2f8a9a93038e50b3b2bf16`)
- uClibc 0.9.33.2: https://uclibc.org/downloads/uClibc-0.9.33.2.tar.bz2
  (SHA-256 `988d2c777e0605fe253d12157f71ec68f25d1bb8428725d2b7460bf9977e1662`)
- Build scripts and configuration: Aboriginal Linux mercurial revision 1535,
  as recorded in https://busybox.net/downloads/binaries/1.20.0/MANIFEST;
  mirrored at https://github.com/landley/aboriginal

## update-binary

The Android recovery updater (edify interpreter), a statically linked ARM
binary from Android's `bootable/recovery` of the Android 4 era. It includes
bionic, zlib, bzip2 and bspatch, all under permissive licenses. Android's
code is licensed under the Apache License 2.0; see `LICENSE.AOSP`. Source:
https://android.googlesource.com/platform/bootable/recovery

The binary also contains a small vendor module (`bootable/recovery/duadummy`)
whose source we have not identified.

## testkey.x509.pem, testkey.pk8

Android's public test key, which this phone's stock recovery trusts. Both files
are byte-identical to `target/product/security/testkey.*` in
https://android.googlesource.com/platform/build, licensed under the Apache
License 2.0; see `LICENSE.AOSP`.

## Where these copies came from

All four files were taken from the Gerda system repository,
https://gitlab.com/project-pris/system, at revision
`0b8d4b1e99efb570e6770b59384c850c1b788d96` (`tools/stockerize/update-binary`,
`src/system/bin/busybox` and `tools/keys/testkey.*`).
