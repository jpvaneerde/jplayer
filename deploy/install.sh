#!/usr/bin/env bash
# JPlay installer for Raspberry Pi OS Lite (Bookworm or newer, 64-bit). Run it again at any time: it keeps your
# settings, and changes the roles when you give other ones.
#
#   sudo apt install -y git
#   sudo git clone https://github.com/jpvaneerde/jplayer /opt/jplayer
#   sudo bash /opt/jplayer/deploy/install.sh --role player,display        # or: player | display | hub | both | all
#
# Roles (any mix, on one Pi):
#   player    the music player (radio, Plex, CD, Spotify Connect, Bluetooth) on port 8080
#   display   JPlaying Now (what plays, lyrics, the microphone) on port 5000, and on the Pi's own screen
#   hub       the house's plays (JPlay Spins) and lyrics, on port 8090. One Pi in the house.
#
# Options:
#   --role R[,R...]        the roles (default: the ones this Pi has, else player,display)
#   --restore FILE         put back settings and history saved on an old Pi with deploy/backup-old.sh
#   --no-kiosk             don't show the display on the Pi's own screen (it can be switched on in Settings)
#   --units-only           only (re)install the systemd files (the updater uses this)
#   SKIP_SPOTIFY=1, SKIP_BLUETOOTH=1   (environment) leave those out of the player
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO/.venv"
ROLES_FILE=/etc/jplay/roles
STATE=/var/lib/jplay
ROLES="" RESTORE="" KIOSK=1 UNITS_ONLY=""

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
has() { [[ " $ROLES " == *" $1 "* ]]; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role|--roles) ROLES="$2"; shift 2 ;;
    --role=*|--roles=*) ROLES="${1#*=}"; shift ;;
    --restore) RESTORE="$2"; shift 2 ;;
    --no-kiosk) KIOSK=""; shift ;;
    --units-only) UNITS_ONLY=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "Run this with sudo." >&2; exit 1; }

# Which roles: as given (comma or space separated; both = player,display; all = every role), else as before
if [[ -z "$ROLES" && -f "$ROLES_FILE" ]]; then ROLES="$(cat "$ROLES_FILE")"; fi
ROLES="${ROLES:-player,display}"
ROLES="$(echo "$ROLES" | tr ',' ' ' | sed 's/\bboth\b/player display/; s/\ball\b/player display hub/')"
clean=""
for r in player display hub; do [[ " $ROLES " == *" $r "* ]] && clean+="$r "; done
for r in $ROLES; do [[ "$r" =~ ^(player|display|hub)$ ]] || { echo "Unknown role '$r': player, display or hub" >&2; exit 2; }; done
ROLES="${clean% }"
[[ -n "$ROLES" ]] || { echo "Give at least one role" >&2; exit 2; }

units() {
  # The systemd files, with the repo's real location
  local u
  for u in jplay-player jplay-display jplay-hub jplay-kiosk; do
    sed "s#/opt/jplayer#$REPO#g" "$REPO/deploy/$u.service" > "/etc/systemd/system/$u.service"
  done
  install -m 644 "$REPO/deploy/90-jplay-backlight.rules" /etc/udev/rules.d/90-jplay-backlight.rules
  systemctl daemon-reload
}

if [[ -n "$UNITS_ONLY" ]]; then
  units
  exit 0
fi

say "JPlay: $ROLES"
mkdir -p /etc/jplay

# ------------------------------------------------------------------ packages
say "Installing system packages"
pkgs=(python3 python3-venv python3-pip git curl ca-certificates avahi-daemon avahi-utils)
has player && pkgs+=(mpv cd-paranoia eject alsa-utils)
has player && [[ -z "${SKIP_BLUETOOTH:-}" ]] && pkgs+=(bluez bluez-tools bluez-alsa-utils)
# The display: the microphone (PortAudio), radio recognition (ffmpeg), and the screen: cage + Chromium, with
# fonts for every script (Japanese, Korean and Chinese lyrics) and emoji
has display && pkgs+=(libportaudio2 ffmpeg cage wlr-randr fonts-noto-core fonts-noto-cjk)
# A fresh Pi may still be installing its own updates: wait for that, and finish an install that was cut off
for i in $(seq 1 60); do
  fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1 || break
  [[ $i -eq 1 ]] && echo "Waiting for another package install (the Pi's own updates) to finish…"
  sleep 5
done
dpkg --configure -a
apt-get update
apt-get install -y --no-install-recommends "${pkgs[@]}"
# Nice to have (not in every Raspberry Pi OS release): switching an HDMI screen to standby, and emoji
if has display; then
  for p in wlopm fonts-noto-color-emoji; do apt-get install -y --no-install-recommends "$p" >/dev/null 2>&1 || echo "($p isn't available here; carrying on)"; done
fi
if has display && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null; then
  apt-get install -y --no-install-recommends chromium || apt-get install -y --no-install-recommends chromium-browser
fi
if has player && [[ -z "${SKIP_SPOTIFY:-}" ]] && ! command -v librespot >/dev/null; then
  say "Installing librespot (from the Raspotify package)"
  curl -sL https://dtcooper.github.io/raspotify/install.sh | sh
fi
# JPlay starts librespot itself; the stand-alone Raspotify service must not grab the DAC.
systemctl disable --now raspotify.service 2>/dev/null || true

# ------------------------------------------------------------------ the old apps (player-jplay, jplayingnow)
say "Looking for the old player-jplay and JPlaying Now"
for old in jplay.service jplayingnow.service kiosk.service jplay-update-check.timer jplayingnow-update-check.timer; do
  if systemctl list-unit-files "$old" >/dev/null 2>&1 && [[ -f "/etc/systemd/system/$old" ]]; then
    echo "Switching off the old $old (its files stay where they are)"
    systemctl disable --now "$old" 2>/dev/null || true
  fi
done
rm -f /etc/sudoers.d/jplay-update /etc/sudoers.d/jplayingnow-update

# ------------------------------------------------------------------ the jplay user
say "The jplay user"
if ! id jplay >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE" --create-home --shell /usr/sbin/nologin jplay
fi
for g in audio cdrom bluetooth video input render; do
  getent group "$g" >/dev/null && usermod -aG "$g" jplay
done
install -d -o jplay -g jplay "$STATE"

# ------------------------------------------------------------------ settings and history from the old setup
migrate_display() {   # $1 = an old nowplaying_app folder
  local src="$1" dst="$STATE/display"
  install -d -o jplay -g jplay "$dst"
  for f in config.json data/history.db data/lyrics.db; do
    if [[ -f "$src/$f" && ! -f "$dst/$(basename "$f")" ]]; then
      cp "$src/$f" "$dst/$(basename "$f")" && echo "Took over the old JPlaying Now's $(basename "$f")"
    fi
  done
  chown -R jplay:jplay "$dst"
}
if [[ -n "$RESTORE" ]]; then
  say "Restoring $RESTORE"
  tmp="$(mktemp -d)"
  tar -xzf "$RESTORE" -C "$tmp"
  [[ -f "$tmp/etc/jplay/config.toml" && ! -f /etc/jplay/config.toml ]] && install -m 640 -g jplay "$tmp/etc/jplay/config.toml" /etc/jplay/config.toml && echo "Player settings restored"
  [[ -f "$tmp/var/lib/jplay/state.json" && ! -f "$STATE/state.json" ]] && install -o jplay -g jplay -m 644 "$tmp/var/lib/jplay/state.json" "$STATE/state.json" && echo "Player favourites restored"
  [[ -d "$tmp/var/lib/jplay/librespot" && ! -d "$STATE/librespot" ]] && cp -r "$tmp/var/lib/jplay/librespot" "$STATE/" && chown -R jplay:jplay "$STATE/librespot"
  [[ -d "$tmp/jplayingnow" ]] && migrate_display "$tmp/jplayingnow"
  rm -rf "$tmp"
fi
for old in /home/*/jplayingnow/nowplaying_app; do
  [[ -d "$old" ]] && has display && migrate_display "$old"
done

# ------------------------------------------------------------------ Python
say "Python packages (in $VENV)"
extras="$(echo "$ROLES" | tr ' ' ',')"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip wheel
"$VENV/bin/pip" install --quiet -e "$REPO[$extras]"
echo "$ROLES" > "$ROLES_FILE"

# ------------------------------------------------------------------ player
if has player; then
  say "Player"
  if [[ ! -f /etc/jplay/config.toml ]]; then
    install -m 640 -g jplay "$REPO/config.example.toml" /etc/jplay/config.toml
    sed -i "s/^name = \"JPlay\"/name = \"$(hostname)\"/" /etc/jplay/config.toml
    echo "Created /etc/jplay/config.toml: add your Plex address and token there."
  else
    echo "Keeping /etc/jplay/config.toml"
  fi
  if [[ -z "${SKIP_BLUETOOTH:-}" ]]; then
    # BlueALSA: act as a speaker (A2DP sink) only; JPlay runs bluealsa-aplay itself while Bluetooth plays.
    install -d /etc/systemd/system/bluealsa.service.d
    printf '[Service]\nExecStart=\nExecStart=/usr/bin/bluealsa -S -p a2dp-sink\n' > /etc/systemd/system/bluealsa.service.d/jplay.conf
    systemctl disable --now bluealsa-aplay.service 2>/dev/null || true
    install -m 644 "$REPO/deploy/bt-agent.service" /etc/systemd/system/bt-agent.service
    # Show up as a loudspeaker in phones' Bluetooth lists.
    if grep -q '^#\?Class' /etc/bluetooth/main.conf; then
      sed -i 's/^#\?Class *=.*/Class = 0x200414/' /etc/bluetooth/main.conf
    else
      sed -i '/^\[General\]/a Class = 0x200414' /etc/bluetooth/main.conf
    fi
  fi
fi

# ------------------------------------------------------------------ systemd
say "Services"
units
for role in player display hub; do
  if has "$role"; then
    systemctl enable "jplay-$role.service" >/dev/null 2>&1
    systemctl restart "jplay-$role.service"
    echo "jplay-$role: on"
  else
    systemctl disable --now "jplay-$role.service" >/dev/null 2>&1 || true
  fi
done
if has player && [[ -z "${SKIP_BLUETOOTH:-}" ]]; then
  systemctl enable --now bluetooth.service bluealsa.service bt-agent.service >/dev/null 2>&1 || true
  systemctl restart bluetooth.service bluealsa.service || true
fi
udevadm control --reload && udevadm trigger --subsystem-match=backlight || true
if has display; then
  # Start in console mode (no desktop); the kiosk takes the first console
  systemctl set-default multi-user.target >/dev/null
  if [[ -n "$KIOSK" ]]; then
    systemctl enable jplay-kiosk.service >/dev/null 2>&1
    systemctl restart jplay-kiosk.service || true
    echo "jplay-kiosk: on (the display on this Pi's screen)"
  fi
else
  systemctl disable --now jplay-kiosk.service >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------------ sudo: exactly what the apps may do as root
tmp="$(mktemp)"
cat > "$tmp" <<SUDO
# JPlay (deploy/install.sh): the apps may start their updater, switch the kiosk and SSH, and reboot. Nothing else.
jplay ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block jplayer-update.service, /usr/bin/systemctl start --no-block jplayer-update-check.service
jplay ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now jplay-kiosk, /usr/bin/systemctl disable --now jplay-kiosk
jplay ALL=(root) NOPASSWD: /usr/bin/systemctl enable --now ssh, /usr/bin/systemctl disable --now ssh
jplay ALL=(root) NOPASSWD: /usr/sbin/reboot, /usr/sbin/shutdown
SUDO
visudo -cf "$tmp" >/dev/null
install -m 440 "$tmp" /etc/sudoers.d/jplay
rm -f "$tmp"

# ------------------------------------------------------------------ updates
say "Updates"
bash "$REPO/deploy/install-updater.sh"

say "Done"
ip="$(hostname -I | awk '{print $1}')"
has player  && echo "Player:  http://$(hostname).local:8080   (http://$ip:8080)"
has display && echo "Display: http://$(hostname).local:5000   (http://$ip:5000)"
has hub     && echo "Hub:     http://$(hostname).local:8090   (http://$ip:8090)"
if has player && ! grep -qiE 'hifiberry|USB-Audio' /proc/asound/cards 2>/dev/null; then
  cat <<'TXT'

No DAC board or USB DAC found. A USB DAC just needs to be plugged in and switched on.
For a HiFiBerry DAC2 HD, add these lines to /boot/firmware/config.txt and reboot:
    dtoverlay=hifiberry-dacplushd
    dtparam=audio=off
TXT
fi
