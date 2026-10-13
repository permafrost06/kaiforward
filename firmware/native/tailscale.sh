#!/system/bin/sh
# Init supervises this process; it supervises tailscaled and Android Wi-Fi routes.
PATH=/system/bin:/system/xbin
export PATH
umask 077
TS_DIR=/data/kaiforward/tailscale
export SSL_CERT_FILE="$TS_DIR/ca-certificates.crt"
export STATE_DIRECTORY="$TS_DIR/state"
export TMPDIR="$TS_DIR/tmp"
export GOMEMLIMIT=64MiB
DAEMON_PID=

alive() {
    [ -n "$DAEMON_PID" ] && [ -r "/proc/$DAEMON_PID/stat" ] || return 1
    [ "$(busybox awk '{print $3}' "/proc/$DAEMON_PID/stat")" != Z ] &&
        kill -0 "$DAEMON_PID" 2>/dev/null
}

wifi_route() {
    ip -4 route show table wlan0 2>/dev/null | busybox grep -q '^default ' || return 1
    rules=$(ip -4 rule show) || return 1
    if printf '%s\n' "$rules" | busybox grep -q '^5200:.*fwmark 0x80000/0xff0000 lookup wlan0'; then
        return 0
    fi
    # Reserve this priority without interfering with unrelated routing rules.
    printf '%s\n' "$rules" | busybox grep -q '^5200:' && return 1
    ip -4 rule add pref 5200 fwmark 0x80000/0xff0000 lookup wlan0
}

cleanup() {
    trap - INT TERM
    if [ -n "$DAEMON_PID" ]; then
        kill "$DAEMON_PID" 2>/dev/null
        wait "$DAEMON_PID" 2>/dev/null
    fi
    if ip -4 rule show | busybox grep -q '^5200:.*fwmark 0x80000/0xff0000 lookup wlan0'; then
        ip -4 rule del pref 5200 fwmark 0x80000/0xff0000 lookup wlan0
    fi
    rm -f "$TS_DIR/tailscaled.pid" "$TS_DIR/supervisor.pid"
    exit 0
}
trap cleanup INT TERM

mkdir -p "$TS_DIR" "$STATE_DIRECTORY" "$TMPDIR" || exit 1
chmod 700 "$TS_DIR" "$STATE_DIRECTORY" "$TMPDIR"
cd "$TS_DIR" || exit 1
# Keep manual starts and init restarts from launching duplicate daemons.
if [ "${1:-}" != --locked ]; then
    # Android's mksh closes script-opened descriptors when executing a command.
    exec busybox flock -n "$TS_DIR/service.lock" /system/bin/sh "$0" --locked
fi
echo $$ >"$TS_DIR/supervisor.pid"

while :; do
    if ! alive; then
        [ -z "$DAEMON_PID" ] || wait "$DAEMON_PID" 2>/dev/null
        DAEMON_PID=
        if [ -x "$TS_DIR/tailscaled" ] && [ -r "$SSL_CERT_FILE" ] &&
            [ -e /dev/tun ] && wifi_route; then
            mkdir -p /dev/net
            [ -e /dev/net/tun ] || ln -s /dev/tun /dev/net/tun
            # Retain the preceding session's log and start a fresh one.
            [ ! -f "$TS_DIR/tailscaled.log" ] || mv -f "$TS_DIR/tailscaled.log" "$TS_DIR/tailscaled.log.previous"
            "$TS_DIR/tailscaled" --tun=tailscale0 --statedir="$STATE_DIRECTORY" \
                --socket="$TS_DIR/tailscaled.sock" >"$TS_DIR/tailscaled.log" 2>&1 &
            DAEMON_PID=$!
            echo "$DAEMON_PID" >"$TS_DIR/tailscaled.pid"
        fi
    else
        # netd can recreate its routing tables after Wi-Fi reconnects.
        wifi_route >/dev/null 2>&1
    fi
    sleep 10
done
