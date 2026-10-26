#!/system/bin/sh
# Runs as root: Android netd's control socket is inaccessible to dhcp UID.
PATH=/system/bin:/system/xbin
export PATH
# Also used as BusyBox ntpd's callback. Keep this ahead of DHCP's interface
# check: ntpd supplies step/stratum/periodic events without an interface.
case "$1" in
    step|stratum)
        touch /data/kaiforward/sync
        exit $?
        ;;
    periodic) exit 0 ;;
esac
[ "$interface" = wlan0 ] || exit 1
case "$1" in
    deconfig)
        ndc network destroy 100 >/dev/null 2>&1
        ifconfig wlan0 0.0.0.0
        ;;
    bound|renew)
        prefix=$(kaiforward-native --network-prefix "$ip" "$subnet") || exit 1
        ifconfig wlan0 "$ip" netmask "$subnet" up || exit 1
        ndc network destroy 100 >/dev/null 2>&1
        ndc network create 100 || exit 1
        ndc network interface add 100 wlan0 || exit 1
        ndc network route add 100 wlan0 "$prefix" || exit 1
        gateway=${router%% *}
        if [ -n "$gateway" ]; then
            ndc network route add 100 wlan0 0.0.0.0/0 "$gateway" || exit 1
        fi
        ndc resolver setnetdns 100 "$domain" $dns || exit 1
        ndc network default set 100 || exit 1
        count=1
        for dnsaddr in $dns; do
            [ "$count" -le 4 ] || break
            setprop net.dns$count "$dnsaddr"
            count=$((count + 1))
        done
        while [ "$count" -le 4 ]; do
            setprop net.dns$count ""
            count=$((count + 1))
        done
        ;;
esac
