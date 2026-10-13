#!/system/bin/sh
# Nokia-specific bounded uplink experiment. Never places/answers/holds a call.
# voice1/voice2 select native voice sessions, not a hardcoded carrier or SIM slot.
set -eu

SESSION=${1:-voice1}
GAIN_MODE=${2:-default}
case "$GAIN_MODE" in
  default|half) ;;
  *) echo 'Gain mode must be default or half.' >&2; exit 2 ;;
esac
case "$SESSION" in
  voice1)
    CONTROL='Incall_Music Audio Mixer MultiMedia2'
    VOICE_DEVICES='2 27'
    ;;
  voice2)
    CONTROL='Incall_Music_2 Audio Mixer MultiMedia2'
    VOICE_DEVICES='13 28'
    ;;
  *) echo 'Choose native session voice1 or voice2.' >&2; exit 2 ;;
esac

PROBE_ROOT=/data/local/tmp/kaiforward-audio-probe
LOCK="$PROBE_ROOT/injection.lock"
INPUT=/data/local/tmp/kaiforward-injection-test.wav
if [ "$(id -u)" != 0 ]; then
  echo 'Temporary root access is required.' >&2
  exit 1
fi

ACTIVE=0
for DEVICE in $VOICE_DEVICES; do
  STATUS="/proc/asound/card0/pcm${DEVICE}p/sub0/status"
  if [ -r "$STATUS" ] && /system/bin/busybox grep -q '^state: RUNNING' "$STATUS"; then
    ACTIVE=1
  fi
done
if [ "$ACTIVE" != 1 ]; then
  echo "No running voice PCM for $SESSION. Connect a manual test call first." >&2
  exit 1
fi

# Only inject into the modem. Refuse existing routes to speakers, headphones,
# other sinks, or voice sessions rather than playing through local hardware.
if tinymix | /system/bin/busybox grep 'Audio Mixer MultiMedia2 ' | /system/bin/busybox grep -q ' On$'; then
  echo 'MultiMedia2 already has an active playback route; refusing to replace it.' >&2
  exit 1
fi
if [ "$(cat /proc/asound/card0/pcm1p/sub0/status)" != closed ]; then
  echo 'MultiMedia2 playback is already open; refusing to take it over.' >&2
  exit 1
fi
if [ ! -r "$INPUT" ]; then
  echo 'The prepared six-second test WAV is missing.' >&2
  exit 1
fi

umask 077
mkdir -p "$PROBE_ROOT"
if ! mkdir "$LOCK"; then
  echo 'Another injection holds the lock. Inspect it before retrying.' >&2
  exit 1
fi
PREVIOUS=$(tinymix "$CONTROL")
case "$PREVIOUS" in
  "$CONTROL: Off") PREVIOUS_VALUE=0 ;;
  "$CONTROL: On") PREVIOUS_VALUE=1 ;;
  *) rmdir "$LOCK"; echo 'Unexpected mixer value; aborting.' >&2; exit 1 ;;
esac

RESTORE_NEEDED=0
GAIN_RESTORE_NEEDED=0
cleanup() {
  RESULT=$?
  trap - 0 1 2 15
  # Gain belongs to the open PCM runtime. Closing that stream discards it;
  # only restore explicitly if the same probe stream is still open.
  if [ "$GAIN_RESTORE_NEEDED" = 1 ] && [ "$(cat /proc/asound/card0/pcm1p/sub0/status)" != closed ]; then
    if ! tinymix 'Playback 1 Volume' "$PREVIOUS_GAIN"; then
      echo 'Playback gain restoration failed.' >&2
      RESULT=1
    fi
  fi
  if [ "$RESTORE_NEEDED" = 1 ]; then
    if ! tinymix "$CONTROL" "$PREVIOUS_VALUE"; then
      echo 'Mixer restoration failed; inspect the injection control before retrying.' >&2
      RESULT=1
    fi
  fi
  rmdir "$LOCK" || RESULT=1
  exit "$RESULT"
}
trap cleanup 0
trap 'exit 130' 1 2 15

EVIDENCE="$PROBE_ROOT/injection-$SESSION-$(date +%Y%m%d-%H%M%S)-$$"
tinymix > "$EVIDENCE.mixer-before.txt"
echo "PHASE: enabling $CONTROL"
RESTORE_NEEDED=1
tinymix "$CONTROL" 1
echo 'PHASE: injection route enabled'

# Match the stock HAL's in-call music PCM format: mono, 48 kHz, 16-bit.
# The WAV is six seconds; -t and independent watchdogs bound a stalled write.
echo 'PHASE: starting six-second PCM playback'
if [ "$GAIN_MODE" = default ]; then
  /system/bin/busybox timeout -t 14 -s 9 \
    /system/bin/busybox timeout -t 12 -s 2 \
    tinyplay "$INPUT" -D 0 -d 1 -p 240 -n 2 -t 6
else
  /system/bin/busybox timeout -t 14 -s 9 \
    /system/bin/busybox timeout -t 12 -s 2 \
    tinyplay "$INPUT" -D 0 -d 1 -p 240 -n 2 -t 6 &
  PLAYBACK_PID=$!
  RUNNING=0
  ATTEMPT=0
  while [ "$ATTEMPT" -lt 60 ]; do
    if /system/bin/busybox grep -q '^state: RUNNING' /proc/asound/card0/pcm1p/sub0/status; then
      RUNNING=1
      break
    fi
    if ! kill -0 "$PLAYBACK_PID" 2>/dev/null; then
      break
    fi
    ATTEMPT=$((ATTEMPT + 1))
    /system/bin/busybox usleep 50000
  done
  if [ "$RUNNING" != 1 ]; then
    echo 'Playback did not reach RUNNING; leaving gain untouched.' >&2
    wait "$PLAYBACK_PID"
    exit 1
  fi
  echo 'PHASE: live PCM parameters and original playback gain'
  cat /proc/asound/card0/pcm1p/sub0/hw_params
  cat /proc/asound/card0/pcm1p/sub0/status > "$EVIDENCE.pcm-running.txt"
  GAIN_REPORT=$(tinymix 'Playback 1 Volume')
  echo "$GAIN_REPORT"
  PREVIOUS_GAIN=${GAIN_REPORT#Playback 1 Volume: }
  PREVIOUS_GAIN=${PREVIOUS_GAIN%% *}
  case "$PREVIOUS_GAIN" in
    ''|*[!0-9]*) echo 'Unexpected runtime gain; leaving it untouched.' >&2; wait "$PLAYBACK_PID"; exit 1 ;;
  esac
  if [ "$PREVIOUS_GAIN" -gt 8192 ]; then
    echo 'Runtime gain is outside the verified range.' >&2
    wait "$PLAYBACK_PID"
    exit 1
  fi
  GAIN_RESTORE_NEEDED=1
  tinymix 'Playback 1 Volume' 4096
  echo 'PHASE: half gain applied to the open playback stream'
  tinymix 'Playback 1 Volume'
  tinymix > "$EVIDENCE.mixer-during.txt"
  wait "$PLAYBACK_PID"
  # PCM closure releases the per-stream gain; no persistent volume was changed.
  GAIN_RESTORE_NEEDED=0
fi

echo 'PHASE: PCM playback returned; restoring injection route'
tinymix "$CONTROL" "$PREVIOUS_VALUE"
RESTORE_NEEDED=0
echo 'PHASE: injection route restored'
tinymix > "$EVIDENCE.mixer-after.txt"
echo "Injection playback finished; evidence prefix: $EVIDENCE"
