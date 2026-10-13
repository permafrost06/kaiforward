#!/system/bin/sh
# Qualcomm owns firmware/calibration; this service owns the Wi-Fi client.
PATH=/system/bin:/system/xbin
export PATH
umask 077
HOME_DIR=/data/kaiforward
SUPPLICANT_CONFIG=/data/misc/wifi/kaiforward.conf
SUPPLICANT_PID=
DHCP_PID=
alive() {
    [ -n "$1" ] && [ -r "/proc/$1/stat" ] || return 1
    # kill -0 also succeeds for a zombie; inspect the child's state instead.
    state=$(busybox awk '{print $3}' "/proc/$1/stat")
    [ "$state" != Z ] && kill -0 "$1" 2>/dev/null
}
cleanup() {
    [ -z "$DHCP_PID" ] || kill "$DHCP_PID" 2>/dev/null
    [ -z "$SUPPLICANT_PID" ] || kill "$SUPPLICANT_PID" 2>/dev/null
    [ -z "$DHCP_PID" ] || wait "$DHCP_PID" 2>/dev/null
    [ -z "$SUPPLICANT_PID" ] || wait "$SUPPLICANT_PID" 2>/dev/null
    DHCP_PID=
    SUPPLICANT_PID=
    ndc network destroy 100 >/dev/null 2>&1
}
trap 'cleanup; exit 0' INT TERM
mkdir -p /data/misc/wifi/sockets
chown wifi:wifi /data/misc/wifi/sockets
chmod 770 /data/misc/wifi/sockets
scan_tick=0
while :; do
    if kaiforward-native --wifi-config > "$SUPPLICANT_CONFIG.new"; then
        RESTART=0
        if ! cmp -s "$SUPPLICANT_CONFIG" "$SUPPLICANT_CONFIG.new"; then
            chown wifi:wifi "$SUPPLICANT_CONFIG.new"
            chmod 600 "$SUPPLICANT_CONFIG.new"
            mv "$SUPPLICANT_CONFIG.new" "$SUPPLICANT_CONFIG"
            RESTART=1
        else
            rm -f "$SUPPLICANT_CONFIG.new"
        fi
        if [ "$RESTART" = 1 ] || ! alive "$SUPPLICANT_PID" || ! alive "$DHCP_PID"; then
            cleanup
            if [ ! -d /sys/class/net/wlan0 ]; then
                insmod /system/lib/modules/wlan.ko 2>/dev/null
            fi
            if ifconfig wlan0 up; then
                wpa_supplicant -iwlan0 -Dnl80211 -c"$SUPPLICANT_CONFIG" -I/system/etc/wifi/wpa_supplicant_overlay.conf -O/data/misc/wifi/sockets > "$HOME_DIR/wifi.log" 2>&1 &
                SUPPLICANT_PID=$!
                busybox udhcpc -f -i wlan0 -s /system/kaiforward/udhcpc.sh -t 3 -T 3 -A 10 -x hostname:nokia-8110 > "$HOME_DIR/dhcp.log" 2>&1 &
                DHCP_PID=$!
            fi
        fi
    else
        rm -f "$SUPPLICANT_CONFIG.new"
        cleanup
    fi
    if [ "$scan_tick" = 0 ] || [ -f "$HOME_DIR/scan" ]; then
        kaiforward-native --scan-wifi
        rm -f "$HOME_DIR/scan"
    fi
    scan_tick=$(((scan_tick + 1) % 5))
    rm -f "$HOME_DIR/reload"
    sleep 2
done
