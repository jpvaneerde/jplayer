# JPlay for Home Assistant

Shows and controls the JPlay players and the JPlaying Now displays in Home Assistant, and through Home Assistant
Cloud in Google Home.

## Install

**With HACS** (the repo must be public on GitHub): HACS → ⋮ → **Custom repositories**, add
`https://github.com/jpvaneerde/jplayer` as type **Integration**, then download **JPlay** and restart Home
Assistant.

**By hand:** copy this `jplay` folder to `/config/custom_components/jplay` on Home Assistant (e.g. with the Samba
share or the Studio Code Server add-on), then restart Home Assistant. To update, copy it again and restart.

## Add your devices

- **JPlay players** announce themselves on the network. After the restart, they appear under **Settings → Devices &
  services → Discovered**: press **Add**. Or add one by hand: **Add integration → JPlay → JPlay player**, with the Pi's
  address (port 8080).
- **JPlaying Now displays** announce themselves too (version 2 and later): they appear under **Discovered** as well.
  Or by hand: **Add integration → JPlay → JPlaying Now display**, with a name (e.g. "Lounge display") and the Pi's
  address (port 5000).

## What you get

**Per player** (shown as a speaker):

| Entity | What it does |
| --- | --- |
| Media player | What plays (title, artist, album, cover, progress), play/pause/stop/next/previous, volume (when the DAC has a volume control), **source** (Radio, Plex, CD, Spotify, Bluetooth), off = standby. The media browser lists the radio favourites, the CD's tracks and the Plex playlists. |
| Radio station | Pick a radio favourite: it plays at once. Shows the station playing. |
| Output | Which DAC the player uses (or Automatic). |
| Source, Now playing, Station, Audio format, CD, Bluetooth device | Sensors for automations and dashboards. |
| Playing, DAC, CD inserted, Bluetooth connected | On/off sensors. |
| Bluetooth pairing | Makes the player visible to phones for a few minutes. |
| Eject CD, Standby | Buttons. |
| Update | The player's version and the newest release (Settings → Updates), with release notes and Install. |

Changes arrive at once (the player pushes them); while a song plays, the position isn't written to the history
every second.

**Per JPlaying Now display:**

| Entity | What it does |
| --- | --- |
| Screen | Switch the screen on or off. Off stays off until someone touches it, a new song is heard, or you switch it on. |
| Shown player | Which player the display shows. |
| Showing | What's on the screen: the player's song or station, or what the microphone hears. |
| Heard | The song the microphone last recognised (records, CDs). |
| Update | The display's version and the newest release, with release notes and Install. |

**The JPlay hub** (found by itself too, or **Add integration → JPlay → JPlay hub**, port 8090):

| Entity | What it does |
| --- | --- |
| *Room* now playing | Per room: what plays now ("Prince – Let's Go Crazy"), the station, or Quiet, with the cover as picture. Attributes: title, artist, album, kind (record, stream, heard), source, station, which of your records. A new room gets its sensor by itself. |
| *Room* display | Per room: whether its display reports to the hub. |
| Records put on today, Songs played today | Today's counts (from midnight). |
| Radio and streams today, Vinyl today | Hours today. |
| Last record put on | The last record or CD, with its cover, room and number of tracks. |
| Update | The version of the hub's Pi and its newest release. |

**Raspberry Pi OS updates** (once per Pi, on its player, else its display or hub): **System updates** (how many
wait; the packages as an attribute), **Restart needed**, and the buttons **Install system updates** (restarts the Pi
afterwards when needed) and **Restart Pi**. The Pi also installs them by itself at night unless that is switched off
in its settings.

**Updates:** each player, display and hub has an update entity. Settings → Updates lists the ones with a new release;
Install asks the device's own updater, which restarts the app and goes back to the previous version if it doesn't
start (an error then says so). Updates started on the device itself show their progress here too. Install appears
once the updater is set up on the device (deploy/install.sh does that). A player and a display on the same Pi share
one version: installing on one updates both.

## Playing something from automations

`media_player.play_media` with `media_content_type: music` and `media_content_id`:

- `radio:P3`: a radio favourite, by name (or its id)
- `cd:3`: track 3 of the CD in the drive
- `plex:<key>`: a Plex playlist or album (keys are in the media browser)
- an `http(s)://` stream address, or anything from Home Assistant's own media (e.g. text-to-speech)

```yaml
# Weekdays at 7:00: P3 in the kitchen
- alias: Morning radio
  triggers: [{trigger: time, at: "07:00:00"}]
  conditions: [{condition: time, weekday: [mon, tue, wed, thu, fri]}]
  actions:
    - action: media_player.play_media
      target: {entity_id: media_player.kitchen}
      data: {media_content_type: music, media_content_id: "radio:P3"}
```

```yaml
# The display goes dark when everyone has left
- alias: Lounge display off when away
  triggers: [{trigger: state, entity_id: zone.home, to: "0"}]
  actions: [{action: switch.turn_off, target: {entity_id: switch.lounge_display_screen}}]
```

## Google Home (Home Assistant Cloud)

**Settings → Voice assistants → Expose**: add the players' media players (and, if you like, the radio station
pickers or a JPlaying Now screen). In Google Home they appear as speakers: "Hey Google, pause Lounge", "set Lounge to
30 percent", "switch Lounge to CD", "turn off Lounge". Google Home doesn't show the song or the cover. To start a
station by voice, make a Home Assistant script that plays it, expose the script, and say "Hey Google, activate
<script name>" (or put it in a Google routine).

## Tests

`tests_homeassistant/` (in the repo root) runs the integration in a real Home Assistant against a fake player and
a fake JPlaying Now; see `tests_homeassistant/pytest.ini`.
