#!/system/bin/sh
# Keys and binaries stay on /data across firmware updates.
PATH=/system/bin:/system/xbin
export PATH
umask 077
runtime=/data/kaiforward/ssh
[ -x "$runtime/dropbear" ] && [ -s "$runtime/.ssh/authorized_keys" ] || exit 0
[ -f "$runtime/disabled" ] && exit 0
mkdir -p "$runtime/.ssh"
chmod 700 "$runtime" "$runtime/.ssh"
chmod 600 "$runtime/.ssh/authorized_keys"
if [ ! -s "$runtime/host_ed25519" ]; then
    "$runtime/dropbearkey" -t ed25519 -f "$runtime/host_ed25519" >"$runtime/keygen.log" 2>&1 || exit 1
fi
exec "$runtime/dropbear" -F -j -k -p 22 -r "$runtime/host_ed25519" -P "$runtime/dropbear.pid" >>"$runtime/ssh.log" 2>&1
