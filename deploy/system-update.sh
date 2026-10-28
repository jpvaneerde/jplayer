#!/usr/bin/env bash
# Raspberry Pi OS updates for a JPlay Pi, run as root by its own systemd units (never by the apps themselves):
#   system-update.sh check     apt update, and note how many updates wait and whether a restart is needed
#   system-update.sh install   install them (apt full-upgrade); restart the Pi afterwards when it is needed and
#                              the request asks for it (STATE_DIR/system-request.json: {"reboot": true})
#   system-update.sh reboot    restart the Pi
# Progress and results go to STATE_DIR/system-status.json, which the apps show (Settings, Home Assistant).
# Settings files that were changed by hand are kept (--force-confold). New Raspberry Pi OS releases (Bookworm to
# Trixie and so on) are not done here: reinstall for those (deploy/install.sh --restore).
set -uo pipefail

MODE="${1:-check}"
STATE_DIR="${STATE_DIR:-/var/lib/jplay}"
STATUS="$STATE_DIR/system-status.json"
REQUEST="$STATE_DIR/system-request.json"
APP_USER="${APP_USER:-jplay}"
LOCK=/run/lock/jplayer-system-update.lock
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)
LOG=()

say() { LOG+=("$(date '+%H:%M:%S') $*"); echo "$*"; }

# A restart is needed when a new kernel or firmware was installed after the Pi started (or Debian says so)
reboot_needed() {
  [[ -e /run/reboot-required || -e /var/run/reboot-required ]] && return 0
  local booted f
  booted=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))
  for f in /boot/firmware/kernel*.img /boot/firmware/start*.elf /boot/kernel*.img; do
    [[ -e "$f" ]] || continue
    (( $(stat -c %Y "$f") > booted + 60 )) && return 0
  done
  return 1
}

write_status() {
  local state="$1" step="$2" message="$3"
  local tmp; tmp="$(mktemp "$STATE_DIR/.system-status.XXXXXX")"
  local waiting; waiting="$(apt-get -s -o Debug::NoLocking=1 full-upgrade 2>/dev/null | awk '/^Inst /{print $2}')"
  local reboot=0; reboot_needed && reboot=1
  STATE="$state" STEP="$step" MESSAGE="$message" WAITING="$waiting" REBOOT="$reboot" OLD="$STATUS" CHECKED="${CHECKED:-}" \
  LOG_TEXT="$(printf '%s\n' "${LOG[@]:-}" | tail -n 30)" OS="$(. /etc/os-release 2>/dev/null; echo "${PRETTY_NAME:-}")" \
  KERNEL="$(uname -r)" python3 - "$tmp" <<'PY'
import json, os, sys, time
e = os.environ
try:
    s = json.load(open(e["OLD"]))
except Exception:
    s = {}
pkgs = [p for p in e["WAITING"].split() if p]
s.update(state=e["STATE"], step=e["STEP"], message=e["MESSAGE"], updated_at=time.time(), updates=len(pkgs),
         packages=pkgs[:40], reboot_needed=e["REBOOT"] == "1", os=e["OS"], kernel=e["KERNEL"],
         log=[l for l in e["LOG_TEXT"].splitlines() if l])
if e["CHECKED"]:
    s["checked_at"] = time.time()
if e["STATE"] == "installing" and e["STEP"] == "starting":
    s.update(started_at=time.time(), finished_at=None)
if e["STATE"] in ("done", "failed"):
    s["finished_at"] = time.time()
json.dump(s, open(sys.argv[1], "w"), indent=1)
PY
  chmod 644 "$tmp"; chown "$APP_USER" "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$STATUS"
}

do_check() {
  write_status checking fetching "Checking for system updates"
  if ! apt-get update -q >/tmp/jplay-apt-update.log 2>&1; then
    say "apt update failed: $(tail -n 1 /tmp/jplay-apt-update.log)"
    CHECKED=1 write_status idle failed_check "Couldn't reach the package servers"
    return 1
  fi
  CHECKED=1 write_status idle checked "Checked for system updates"
}

do_install() {
  local reboot_wanted
  reboot_wanted="$(python3 -c 'import json,sys; print(1 if json.load(open(sys.argv[1])).get("reboot") else 0)' "$REQUEST" 2>/dev/null || echo 0)"
  write_status installing starting "Installing system updates"
  say "Fetching the package lists"
  apt-get update -q >/tmp/jplay-apt-update.log 2>&1 || say "apt update: $(tail -n 1 /tmp/jplay-apt-update.log)"
  # An install that was cut off before (power cut): finish it first
  dpkg --configure -a >/dev/null 2>&1
  local n; n="$(apt-get -s -o Debug::NoLocking=1 full-upgrade 2>/dev/null | grep -c '^Inst ')"
  say "$n packages to update"
  write_status installing installing "Installing $n updates"
  if ! apt-get "${APT_OPTS[@]}" full-upgrade >/tmp/jplay-apt-upgrade.log 2>&1; then
    say "Failed: $(tail -n 3 /tmp/jplay-apt-upgrade.log | tr '\n' ' ')"
    CHECKED=1 write_status failed installing "Installing the system updates failed (see the log)"
    return 1
  fi
  apt-get "${APT_OPTS[@]}" autoremove --purge >/dev/null 2>&1
  apt-get clean
  say "Installed $n updates"
  if reboot_needed; then
    if [[ "$reboot_wanted" == 1 ]]; then
      say "Restarting the Pi (new kernel or firmware)"
      CHECKED=1 write_status done rebooting "Updated; restarting the Pi"
      sleep 3
      systemctl reboot
      return 0
    fi
    CHECKED=1 write_status done restart_needed "Updated; a restart finishes it"
  else
    CHECKED=1 write_status done done "Updated ($n packages)"
  fi
}

do_reboot() {
  say "Restarting the Pi"
  write_status idle rebooting "Restarting the Pi"
  sleep 2
  systemctl reboot
}

mkdir -p "$STATE_DIR"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "A system update or check is running already"
  exit 0
fi
case "$MODE" in
  check) do_check ;;
  install) do_install ;;
  reboot) do_reboot ;;
  *) echo "usage: $0 check|install|reboot"; exit 2 ;;
esac
