#!/usr/bin/env bash
# Run on a Pi with the OLD player-jplay and/or JPlaying Now, before you reinstall it with Raspberry Pi OS Lite:
#   curl -sL https://raw.githubusercontent.com/jpvaneerde/jplayer/main/deploy/backup-old.sh | sudo bash
#   (or copy this file over and: sudo bash backup-old.sh)
# Makes ~/jplay-backup-<name>.tar.gz with the player's settings, favourites and Spotify login, and JPlaying Now's
# settings, history and lyrics. Copy it off the Pi (e.g. scp), then on the new install:
#   sudo bash /opt/jplayer/deploy/install.sh --role ... --restore jplay-backup-<name>.tar.gz
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run this with sudo"; exit 1; }
out="${SUDO_USER:+/home/$SUDO_USER}"; out="${out:-$HOME}/jplay-backup-$(hostname).tar.gz"
tmp="$(mktemp -d)"
[[ -f /etc/jplay/config.toml ]] && install -D /etc/jplay/config.toml "$tmp/etc/jplay/config.toml"
[[ -f /var/lib/jplay/state.json ]] && install -D /var/lib/jplay/state.json "$tmp/var/lib/jplay/state.json"
[[ -d /var/lib/jplay/librespot ]] && mkdir -p "$tmp/var/lib/jplay" && cp -r /var/lib/jplay/librespot "$tmp/var/lib/jplay/"
for app in /home/*/jplayingnow/nowplaying_app; do
  [[ -d "$app" ]] || continue
  for f in config.json data/history.db data/lyrics.db; do
    [[ -f "$app/$f" ]] && install -D "$app/$f" "$tmp/jplayingnow/$f"
  done
done
if [[ -z "$(ls -A "$tmp")" ]]; then echo "Found nothing of the old apps here."; rm -rf "$tmp"; exit 1; fi
tar -czf "$out" -C "$tmp" .
rm -rf "$tmp"
[[ -n "${SUDO_USER:-}" ]] && chown "$SUDO_USER" "$out"
echo "Saved: $out"
tar -tzf "$out" | sed 's/^/  /'
