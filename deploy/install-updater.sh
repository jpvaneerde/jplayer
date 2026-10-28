#!/usr/bin/env bash
# The safe updater (deploy/install.sh runs this; run it again by itself after changing roles by hand):
#   jplayer-update.service         updates to the version an app asked for, restarts the apps, goes back if one doesn't start
#   jplayer-update-check.service   fetches from GitHub and notes the newest release
#   jplayer-update-check.timer     runs the check 5 minutes after boot and every 6 hours
# and for Raspberry Pi OS itself (deploy/system-update.sh):
#   jplayer-system-check.service/.timer   apt update once a day: how many updates wait, is a restart needed
#   jplayer-system-update.service         installs them (the apps start it: by hand, or at night when nothing plays)
#   jplayer-system-reboot.service         restarts the Pi
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run this with sudo"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ROLES="$(cat /etc/jplay/roles 2>/dev/null || echo "player display")"
NAME=jplayer
STATE_DIR=/var/lib/jplay

services="" health=""
for r in $ROLES; do
  services+="jplay-$r "
  case "$r" in
    player)  health+="http://127.0.0.1:8080/api/v1/health " ;;
    display) health+="http://127.0.0.1:5000/api/version " ;;
    hub)     health+="http://127.0.0.1:8090/api/health " ;;
  esac
done
extras="$(echo "$ROLES" | tr ' ' ',')"

install -d -o jplay -g jplay "$STATE_DIR"
cat > "/etc/$NAME-update.env" <<CONF
APP=JPlay
REPO=$REPO
STATE_DIR=$STATE_DIR
SERVICES="${services% }"
APP_USER=jplay
HEALTH_URLS="${health% }"
DEPS_FILES=pyproject.toml
DEPS_CMD=".venv/bin/pip install --quiet -e .[$extras]"
CONF

for mode in "" "-check"; do
  arg=install; desc="Update JPlay"
  [[ -n "$mode" ]] && { arg=check; desc="Check for JPlay updates"; }
  cat > "/etc/systemd/system/$NAME-update$mode.service" <<UNIT
[Unit]
Description=$desc
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/$NAME-update.env
ExecStart=/bin/bash $REPO/deploy/update.sh $arg
TimeoutStartSec=20min
UNIT
done

cat > "/etc/systemd/system/$NAME-update-check.timer" <<UNIT
[Unit]
Description=Check for JPlay updates a few times a day

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
RandomizedDelaySec=20min
Persistent=true

[Install]
WantedBy=timers.target
UNIT

# Raspberry Pi OS updates
for mode in check update reboot; do
  arg="$mode"; [[ "$mode" == update ]] && arg=install
  desc="Check for Raspberry Pi OS updates"
  [[ "$mode" == update ]] && desc="Install Raspberry Pi OS updates"
  [[ "$mode" == reboot ]] && desc="Restart the Pi (asked for by JPlay)"
  cat > "/etc/systemd/system/$NAME-system-$mode.service" <<UNIT
[Unit]
Description=$desc
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
Environment=STATE_DIR=$STATE_DIR APP_USER=jplay
ExecStart=/bin/bash $REPO/deploy/system-update.sh $arg
TimeoutStartSec=60min
UNIT
done
cat > "/etc/systemd/system/$NAME-system-check.timer" <<UNIT
[Unit]
Description=Check for Raspberry Pi OS updates once a day

[Timer]
OnBootSec=15min
OnUnitActiveSec=1d
RandomizedDelaySec=2h
Persistent=true

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now "$NAME-system-check.timer" >/dev/null
systemctl enable --now "$NAME-update-check.timer" >/dev/null
systemctl start --no-block "$NAME-update-check.service"
echo "Updater installed: Settings shows the version and new releases in a minute."
