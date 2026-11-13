#!/usr/bin/env bash
# Every few minutes (jplay-power-check.timer): has the Pi had too little power or got too hot since it started?
# Too little power (a weak supply or cable) is the most common reason a Pi crashes "for no reason". The firmware
# keeps the answer in one number (vcgencmd get_throttled); when it changes, it is written to the journal and to
# STATE/power-events.log, which survives a restart and which the apps show on the System card.
#   bit 0  under-voltage now          bit 16  under-voltage since start
#   bit 1  speed capped now           bit 17  speed capped since start
#   bit 2  throttled now              bit 18  throttled since start
#   bit 3  soft temperature limit now bit 19  soft temperature limit since start
set -uo pipefail
STATE_DIR="${STATE_DIR:-/var/lib/jplay}"
LOG="$STATE_DIR/power-events.log"
LAST="/run/jplay-power-last"

value=""
f=/sys/devices/platform/soc/soc:firmware/get_throttled
if [[ -r "$f" ]]; then
  value="0x$(cat "$f" 2>/dev/null)"
elif command -v vcgencmd >/dev/null; then
  value="$(vcgencmd get_throttled 2>/dev/null | sed -n 's/^throttled=//p')"
fi
[[ -n "$value" ]] || exit 0
n=$((value))
[[ "$n" -eq 0 ]] && exit 0
[[ -f "$LAST" && "$(cat "$LAST")" == "$value" ]] && exit 0
echo "$value" > "$LAST"

what=()
(( n & 0x1 ))     && what+=("under-voltage now")
(( n & 0x2 ))     && what+=("speed capped now")
(( n & 0x4 ))     && what+=("throttled now")
(( n & 0x8 ))     && what+=("hot now")
(( n & 0x10000 )) && what+=("under-voltage since start")
(( n & 0x20000 )) && what+=("speed capped since start")
(( n & 0x40000 )) && what+=("throttled since start")
(( n & 0x80000 )) && what+=("hot since start")
msg="$(IFS=,; echo "${what[*]}" | sed 's/,/, /g')"
logger -t jplay-power -p user.warning "throttled=$value: $msg"
mkdir -p "$STATE_DIR"
echo "$(date +%s) $value $msg" >> "$LOG"
tail -n 200 "$LOG" > "$LOG.tmp" && mv -f "$LOG.tmp" "$LOG"
chmod 644 "$LOG"
