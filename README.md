# JPlay

Music for the whole house on Raspberry Pis, in one repo. A Pi can have any mix of three roles:

| Role | What it does | Port |
| --- | --- | --- |
| **player** | A headless music player for a good DAC: radio, Plex, CD, Spotify Connect and Bluetooth, controlled from a web page on your phone, a REST API and Home Assistant. | 8080 |
| **display** | JPlaying Now: shows what the room's player plays (or what the microphone hears: records, the TV…) with the cover and the song's lyrics in time with the music, on the Pi's own screen and on any phone. | 5000 |
| **hub** | One Pi in the house keeps everything the rooms report: **JPlay Spins** (every play, which room, which source, the records you put on, listening time per station) and the **lyrics** for every display, with corrections. | 8090 |

They run on Raspberry Pi OS **Lite**: no desktop. The display shows itself on the Pi's screen with
[cage](https://github.com/cage-kiosk/cage) (a one-window Wayland compositor) and Chromium.

JPlay Records (records.jplay.cloud) is no longer needed. When you have it, the hub can use it to tell **which of
your records or CDs** was played (optional, see [The hub](#the-hub)).

```
       phones, Home Assistant
          │           │
   ┌──────▼─────┐ ┌───▼────────┐        ┌───────────────┐
   │ player     │ │ display    │──────► │ hub           │ ◄── every display in the house
   │ :8080      │◄┤ :5000      │ events │ :8090         │     (plays, lyrics)
   │ DAC, CD…   │ │ screen,mic │ lyrics │ Spins, lyrics │
   └────────────┘ └────────────┘        └──────┬────────┘
                                               │ optional: which of your records?
                                        ┌──────▼────────┐
                                        │ JPlay Records │
                                        └───────────────┘
```

## Install

1. Put **Raspberry Pi OS Lite (64-bit)** on the SD card with Raspberry Pi Imager. In its settings set the hostname
   (e.g. `jplaylounge`), your user, Wi-Fi if needed, and switch on SSH.
2. On the Pi:

   ```bash
   sudo apt-get install -y git
   sudo git clone https://github.com/jpvaneerde/jplayer /opt/jplayer
   sudo bash /opt/jplayer/deploy/install.sh --role player,display,hub     # the Pi that is also the hub
   sudo bash /opt/jplayer/deploy/install.sh --role player,display         # every other room
   ```

   Roles: `player`, `display`, `hub` in any mix (`both` = player,display; `all` = all three). Run the installer
   again at any time, with other roles or the same: it keeps your settings.

The installer adds what each role needs (mpv, cd-paranoia, librespot, BlueZ/BlueALSA for the player; cage,
Chromium, ffmpeg, PortAudio and fonts for every script for the display), creates the `jplay` user, a virtualenv in
`/opt/jplayer/.venv`, the services `jplay-player`, `jplay-display`, `jplay-hub` and `jplay-kiosk`, and the updater.

Then:

- **Player:** open `http://<pi>:8080`, tab **Plex**: **Sign in with Plex** and pick your server, or type its address
  and token. (Or set `plex.url` and `plex.token` in `/etc/jplay/config.toml`; what you connect on the page wins.) A USB DAC needs nothing; for a DAC board such as the HiFiBerry DAC2 HD add
  `dtoverlay=hifiberry-dacplushd` and `dtparam=audio=off` to `/boot/firmware/config.txt` and reboot.
- **Display:** open `http://<pi>:5000/settings`: add the players, and under **Room & hub** give the room a name and
  pick the room's player. From then on it reports to the hub.
- **Hub:** open `http://<pi>:8090/settings`: set each room's priority and what plays there (a turntable, a CD
  player), and import the old data (below).

### Coming from player-jplay and JPlaying Now

Your settings, favourites and history come along:

- **Reinstalling a Pi with Raspberry Pi OS Lite:** first, on the old Pi, run `deploy/backup-old.sh` (`sudo bash
  backup-old.sh` after copying it over). It saves the player's settings, favourites and Spotify login and JPlaying
  Now's settings, history and lyrics in `~/jplay-backup-<name>.tar.gz`. Copy that file off the Pi, reinstall, copy it
  back and add `--restore jplay-backup-<name>.tar.gz` to the install command.
- **Installing over the old apps on the same Pi:** the installer switches the old services off and takes over the
  player's `/etc/jplay/config.toml` and `/var/lib/jplay` and JPlaying Now's settings and history by itself.
- **On the hub, Settings → Import:**
  - **From JPlay Records:** every play (with your records), the rooms and the lyrics store. Needs JPlay Records with
    the hub endpoints (`/api/spins/match`, `/api/spins/export`, in the jplayrecords repo) and a listener token
    (JPlay Records → Settings → Listeners).
  - **From an old JPlaying Now** (address and room): what its microphone heard, for plays JPlay Records never got.
    Run this while the old display still runs.
  - **Radio favourites:** copy them from one player to another (also from an old player-jplay).

  Each import can run again; what's already there is skipped.

## The player

Five sources, one at a time; a source that starts on its own (a phone streaming, Spotify picked in the app) takes
over:

- **Radio:** favourites, station search through [Radio Browser](https://www.radio-browser.info), song titles from
  the stream, automatic reconnect.
- **Plex:** browse your library and play the original files (FLAC stays FLAC); tracks are marked as played.
- **CD:** a disc is read into RAM with cd-paranoia as soon as it goes in; playback starts after a second or two,
  then the drive rests. Album and track names from MusicBrainz.
- **Spotify Connect** through librespot, and **Bluetooth** (A2DP speaker) through BlueALSA.

The output is `auto`: a USB DAC while one is switched on, otherwise the DAC board (never HDMI or the headphone
jack). Pick another on the web page (the DAC name at the top right) or set `audio.device` in the config. CD and Plex
play bit-perfect (`hw:` device). The REST API is described at `http://<pi>:8080/api/docs`; live state comes over the
WebSocket `/api/v1/events`. Everything else is in `config.example.toml`.

## The display

Shows the room's player when it plays, else what the microphone hears (Shazam), with the lyrics: in time with the
music when LRCLIB has synced lyrics (also on radio: the display listens to the stream and recognises the song), else
slowly scrolling. History keeps everything the microphone heard; Settings has the players, the microphone, the
screen and the room.

**The screen:** the `jplay-kiosk` service runs cage + Chromium on the first console; switch it with **Display
locally** in Settings. After the set number of quiet minutes the screen goes off, and a touch or a new song wakes
it. A Raspberry Pi touch screen (DSI) is switched with its backlight; an HDMI screen goes to standby with `wlopm`;
when neither works the page goes black instead. Rotation in Settings uses `wlr-randr`; if your screen doesn't turn
under cage, rotate it at boot instead: for HDMI add `video=HDMI-A-1:1920x1080@60,rotate=90` (your screen's resolution)
to the line in `/boot/firmware/cmdline.txt`; for a touch screen use its rotation setting in `/boot/firmware/config.txt`.

## The hub

One Pi in the house (any role mix). The displays find it on the network by themselves (`_jplay-hub._tcp`); type
its address in a display's Settings only if they don't. While the hub is away, the displays keep their reports in
an outbox (two weeks) and use their own copy of the lyrics or ask LRCLIB themselves.

- **Spins** (`http://<hub>:8090/`): what plays in each room now, the records you put on, every play by day (filter
  by kind, room, search), and totals: records put on, hours of vinyl, songs streamed, listening time per station,
  top artists and records. The rules are JPlay Records' rules: a song heard in several rooms at once is one play,
  in the room with the highest priority; while the room's player plays, a song is filed under its source (a CD in
  the player is a CD); the player's own song info adds what the microphone missed, and on radio the microphone wins
  when they disagree. Correct a play with ⋯ (not my record, it was a record/CD, this copy, delete).
- **Lyrics** (`/lyrics`): every song a display asked for. Shift a song's timing (on every display at once), paste
  lyrics by hand (LRC with times, or plain text: never overwritten), or look a song up again.
- **Settings** (`/settings`): rooms (priority, what plays there), the displays that report, JPlay Records, import,
  and updates.

**JPlay Records (optional):** connect it under Settings with its address and a listener token, and songs heard
while no player plays are matched against your collection (records and CDs in a format that room can play), just as
before; a CD in a player gets its copy too. Without it, those songs are "heard" and a CD is "a CD" (spins then group
by album). Plays are matched again every few hours, so a JPlay Records that was away catches up. With **Keep JPlay
Records' Spins up to date** (on by default) the hub also sends every play to JPlay Records, so its Spins pages and
record pages keep counting; corrections and deletions made on the hub follow (JPlay Records needs the
`/api/spins/hub-plays` endpoint for this). The hub fetches the
covers of your records once with the same token. This needs the endpoints added to jplayrecords in
`server/spins.ts` and `server/routes/spins.ts` (`matchForHub`, `exportForHub`) and covers that accept a listener
token (`server/index.ts`).

There are no passwords: like the players and displays, the hub is for your home network only.

## Updates

One version for the whole repo (git tags `vX.Y.Z`), one updater per Pi: an update restarts every role on the Pi
and goes back to the previous version when one of them doesn't start. Every app shows the version and new releases
(player: Output → Updates; display and hub: Settings), and Home Assistant has an update entity per player and display.
Updates can install themselves at night (03:00–05:00, when nothing plays).

**Raspberry Pi OS updates** are handled the same way, by `deploy/system-update.sh` (as root, from its own
services): once a day it checks how many updates wait, and at night (03:00–05:00, when nothing plays) it installs
them and restarts the Pi when a new kernel or firmware needs it. Settings files you changed by hand are kept. The
player's Output page, the display's and the hub's Settings show "System (Raspberry Pi OS)" with Install updates,
Restart now and the automatic switch; Home Assistant shows "System updates", "Restart needed" and two buttons, once
per Pi. New Raspberry Pi OS releases (Bookworm to Trixie and so on) aren't done in place: reinstall for those
(`deploy/backup-old.sh`-style backup of `/etc/jplay` and `/var/lib/jplay`, then `install.sh --restore`).

Release from your PC (PowerShell, in the repo): `.\tools\release.ps1` (asks for the version and notes, tags and
pushes). The Pis see it within 6 hours, or at once with **Check now**.

The updater only fast-forwards: files changed by hand on a Pi stop it (nothing changes). Changed systemd files are
put in place by the updater itself (it runs `deploy/install.sh --units-only`).

## Home Assistant

`custom_components/jplay`: players and displays as devices (players and displays announce themselves; Settings →
Devices & services → Discovered). See [its README](custom_components/jplay/README.md). Install with HACS (custom
repository `https://github.com/jpvaneerde/jplayer`, type Integration) or copy the folder to
`/config/custom_components/jplay`.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[player,display,hub,dev]"
.venv/bin/python -m pytest                       # player, display and hub
JPLAY_STATE=/tmp/jplay .venv/bin/python -m jplay.hub          # http://localhost:8090
JPLAY_STATE=/tmp/jplay .venv/bin/python -m jplay.display      # http://localhost:5000
.venv/bin/python -m jplay.player --config config.example.toml # http://localhost:8080 (set audio.device = "null")
```

On a PC every role is on (`/etc/jplay/roles` decides on a Pi) and state goes to `~/.local/state/jplay` (or
`JPLAY_STATE`). The Home Assistant tests need their own environment: see `tests_homeassistant/pytest.ini`.

```
jplay/common/    shared: updates, paths and roles, network announcements and discovery, LRCLIB matching, the hub's address
jplay/player/    the player (FastAPI, mpv, sources)
jplay/display/   JPlaying Now (FastAPI, Shazam, screen, outbox to the hub)
jplay/hub/       the hub (FastAPI, SQLite: Spins, lyrics, rooms, import, JPlay Records client)
web/player|display|hub   the web pages
deploy/          install.sh (roles), update.sh and install-updater.sh, systemd units, cage kiosk, backup-old.sh
custom_components/jplay  the Home Assistant integration
tests/, tests_homeassistant/, tools/release.ps1
```
