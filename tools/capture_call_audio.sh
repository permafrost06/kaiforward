#!/system/bin/sh
# Device-specific experimental downlink capture; never places or answers a call.
# Run as root only after manually connecting a test call on this Nokia.
set -eu

SECONDS_TO_CAPTURE=${1:-12}
case "$SECONDS_TO_CAPTURE" in
  5|6|7|8|9|10|11|12|13|14|15) ;;
  *) echo 'Capture duration must be 5–15 seconds.' >&2; exit 2 ;;
esac

ROOT=/data/local/tmp/kaiforward-audio-probe
LOCK="$ROOT/capture.lock"
CONTROL='MultiMedia1 Mixer VOC_REC_DL'

if [ "$(id -u)" != 0 ]; then
  echo 'Temporary root access is required.' >&2
  exit 1
fi

# Voice PCM endpoints, not the multimedia PCM opened by this probe.
ACTIVE=0
for DEVICE in 2 13 15 16 26 27 28; do
  STATUS="/proc/asound/card0/pcm${DEVICE}p/sub0/status"
  if [ -r "$STATUS" ] && /system/bin/busybox grep -q '^state: RUNNING' "$STATUS"; then
    ACTIVE=1
  fi
done
if [ "$ACTIVE" != 1 ]; then
  echo 'No running voice PCM found. Connect a manual test call first.' >&2
  exit 1
fi

# Avoid mixing microphone input or taking over another multimedia recording.
if tinymix | /system/bin/busybox grep 'MultiMedia1 Mixer ' | /system/bin/busybox grep -q ' On$'; then
  echo 'MultiMedia1 already has an active capture route; refusing to replace it.' >&2
  exit 1
fi
if [ "$(cat /proc/asound/card0/pcm0c/sub0/status)" != closed ]; then
  echo 'MultiMedia1 capture is already open; refusing to take it over.' >&2
  exit 1
fi

umask 077
mkdir -p "$ROOT"
if ! mkdir "$LOCK"; then
  echo 'Another capture holds the lock. Inspect it before retrying.' >&2
  exit 1
fi

PREVIOUS=$(tinymix "$CONTROL")
case "$PREVIOUS" in
  "$CONTROL: Off") PREVIOUS_VALUE=0 ;;
  "$CONTROL: On") PREVIOUS_VALUE=1 ;;
  *) rmdir "$LOCK"; echo 'Unexpected mixer value; aborting.' >&2; exit 1 ;;
esac

RESTORE_NEEDED=0
cleanup() {
  RESULT=$?
  trap - 0 1 2 15
  if [ "$RESTORE_NEEDED" = 1 ]; then
    if ! tinymix "$CONTROL" "$PREVIOUS_VALUE"; then
      echo 'Mixer restoration failed; inspect the downlink control before retrying.' >&2
      RESULT=1
    fi
  fi
  rmdir "$LOCK" || RESULT=1
  exit "$RESULT"
}
trap cleanup 0
trap 'exit 130' 1 2 15

OUTPUT="$ROOT/downlink-$(date +%Y%m%d-%H%M%S)-$$.wav"
tinymix > "$OUTPUT.mixer-before.txt"
RESTORE_NEEDED=1
tinymix "$CONTROL" 1

# tinycap has a native duration limit; independent signal and kill bounds also
# stop a stalled read. Only the child receives these signals, so this shell can
# restore the route even if the capture fails. Forced termination may leave an
# incomplete WAV; inspect it before treating it as evidence.
/system/bin/busybox timeout -t 22 -s 9 \
  /system/bin/busybox timeout -t 20 -s 2 \
  tinycap "$OUTPUT" -D 0 -d 0 -c 1 -r 8000 -b 16 -p 160 -n 4 -t "$SECONDS_TO_CAPTURE"

tinymix "$CONTROL" "$PREVIOUS_VALUE"
RESTORE_NEEDED=0
tinymix > "$OUTPUT.mixer-after.txt"
echo "Capture saved: $OUTPUT"
