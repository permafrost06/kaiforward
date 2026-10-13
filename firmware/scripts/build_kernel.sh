#!/bin/sh
# Run inside the compiler environment (native x86_64, or muvm/FEX on this PC).
set -eu
umask 077
PROJECT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
BUILD_ROOT=${GATEWAY_BUILD_ROOT:-"$PROJECT/data/gateway-build"}
KERNEL="$BUILD_ROOT/kernel"
OUTPUT="$BUILD_ROOT/kernel-out-gcc48"
CROSS_PREFIX="$BUILD_ROOT/toolchain/bin/arm-eabi-"
WLAN="$KERNEL/drivers/net/wireless/prima"
export KBUILD_BUILD_USER=kaiforward KBUILD_BUILD_HOST=gateway-builder
export KBUILD_BUILD_TIMESTAMP='Mon Oct 12 00:00:00 UTC 2026'
export KBUILD_BUILD_VERSION=1
make_kernel() {
    make -C "$KERNEL" O="$OUTPUT" ARCH=arm CROSS_COMPILE="$CROSS_PREFIX" \
        CC="${CROSS_PREFIX}gcc" HOSTCC='gcc -std=gnu11 -fcommon' "$@"
}
make_kernel olddefconfig > "$BUILD_ROOT/kernel-config-gcc48.log" 2>&1
make_kernel -j"${GATEWAY_JOBS:-6}" zImage modules > "$BUILD_ROOT/kernel-build-gcc48.log" 2>&1
make_kernel -j"${GATEWAY_JOBS:-6}" M="$WLAN" WLAN_ROOT="$WLAN" MODNAME=wlan \
    CONFIG_PRONTO_WLAN=m KCFLAGS=-DWLAN_OPEN_SOURCE modules > "$BUILD_ROOT/wlan-build-gcc48.log" 2>&1
printf 'Kernel and WLAN module build completed\n'
