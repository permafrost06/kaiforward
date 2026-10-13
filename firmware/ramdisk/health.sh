#!/system/bin/sh
# Local, private heartbeat. This bootstrap exposes no network listener.
set -eu
umask 077
DIRECTORY=/data/kaiforward
mkdir -p "$DIRECTORY"
BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)
TEMPORARY="$DIRECTORY/health.json.$$"
trap 'rm -f "$TEMPORARY"' 0
trap 'exit 0' 1 2 15
while :; do
    read UPTIME UNUSED < /proc/uptime
    UPTIME=${UPTIME%%.*}
    ACTIVE=0
    for DEVICE in 2 13 15 16 26 27 28; do
        STATUS="/proc/asound/card0/pcm${DEVICE}p/sub0/status"
        if [ -r "$STATUS" ] && /system/bin/busybox grep -q '^state: RUNNING' "$STATUS"; then
            ACTIVE=$((ACTIVE + 1))
        fi
    done
    printf '{"schema":1,"component":"kaiforward-health","boot_id":"%s","uptime_seconds":%s,"active_voice_pcm_count":%s}\n' \
        "$BOOT_ID" "$UPTIME" "$ACTIVE" > "$TEMPORARY"
    mv "$TEMPORARY" "$DIRECTORY/health.json"
    sleep 60
done
