"""Sensors for players and JPlaying Now displays."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import KIND_PLAYER, SOURCE_NAMES
from .entity import NowEntity, PlayerEntity, song

STANDBY = "Standby"
CD_STATES = {"empty": "No disc", "reading-toc": "Reading", "ready": "Ready", "data": "Data disc", "error": "Error"}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    kind = entry.runtime_data.kind
    if kind == KIND_PLAYER:
        async_add_entities([SourceSensor(entry), NowPlayingSensor(entry), StationSensor(entry), FormatSensor(entry),
                            BluetoothDeviceSensor(entry), CdSensor(entry)])
    else:
        async_add_entities([ShowingSensor(entry), HeardSensor(entry)])


# ==================================================================== players
class SourceSensor(PlayerEntity, SensorEntity):
    _attr_name = "Source"
    _attr_icon = "mdi:import"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STANDBY, *SOURCE_NAMES.values()]

    def __init__(self, entry) -> None:
        super().__init__(entry, "source")

    @property
    def native_value(self) -> str:
        return SOURCE_NAMES.get(self.active, STANDBY) if self.active else STANDBY


class NowPlayingSensor(PlayerEntity, SensorEntity):
    """'Artist – Title' while a song is known (handy for automations and Google Home)."""

    _attr_name = "Now playing"
    _attr_icon = "mdi:music-note"

    def __init__(self, entry) -> None:
        super().__init__(entry, "now_playing")

    @property
    def native_value(self) -> str | None:
        if not self.active or self.np.get("state") not in ("playing", "paused", "loading"):
            return None
        title = self.np.get("title") or ""
        if self.active == "radio" and (not self.np.get("artist") or title == self.extra.get("station")):
            return None   # the station names no song
        return song(title, self.np.get("artist"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.native_value is None:
            return {}
        return {"title": self.np.get("title"), "artist": self.np.get("artist"), "album": self.np.get("album") or None,
                "source": SOURCE_NAMES.get(self.active, self.active)}


class StationSensor(PlayerEntity, SensorEntity):
    _attr_name = "Station"
    _attr_icon = "mdi:radio-tower"

    def __init__(self, entry) -> None:
        super().__init__(entry, "station_name")

    @property
    def native_value(self) -> str | None:
        return (self.extra.get("station") or None) if self.active == "radio" else None


class FormatSensor(PlayerEntity, SensorEntity):
    """Codec, sample rate and bitrate of what plays."""

    _attr_name = "Audio format"
    _attr_icon = "mdi:waveform"

    def __init__(self, entry) -> None:
        super().__init__(entry, "format")

    @property
    def native_value(self) -> str | None:
        if not self.active:
            return None
        x, parts = self.extra, []
        codec = str(x.get("codec") or "").upper()
        if self.active == "cd":
            parts.append("16-bit · 44.1 kHz")
        if codec:
            parts.append(codec)
        if x.get("samplerate") and self.active != "cd":
            parts.append(f"{x['samplerate'] / 1000:g} kHz")
        if x.get("bitrate") and codec not in ("FLAC", "ALAC", "WAV"):
            parts.append(f"{x['bitrate']} kbps")
        return " · ".join(parts) or None


class BluetoothDeviceSensor(PlayerEntity, SensorEntity):
    _attr_name = "Bluetooth device"
    _attr_icon = "mdi:bluetooth-audio"

    def __init__(self, entry) -> None:
        super().__init__(entry, "bt_device")

    @property
    def available(self) -> bool:
        return super().available and bool(self.src("bluetooth").get("available"))

    @property
    def native_value(self) -> str | None:
        connected = [d["name"] for d in self.details("bluetooth").get("devices") or [] if d.get("connected")]
        return ", ".join(connected) or None


class CdSensor(PlayerEntity, SensorEntity):
    _attr_name = "CD"
    _attr_icon = "mdi:disc"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(CD_STATES.values())

    def __init__(self, entry) -> None:
        super().__init__(entry, "cd")

    @property
    def available(self) -> bool:
        return super().available and bool(self.src("cd").get("available"))

    @property
    def native_value(self) -> str | None:
        return CD_STATES.get(self.details("cd").get("status") or "empty")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        disc = self.details("cd").get("disc") or {}
        if not disc:
            return {}
        return {"album": disc.get("album") or None, "artist": disc.get("artist") or None,
                "tracks": len(disc.get("tracks") or [])}


# ==================================================================== JPlaying Now
class ShowingSensor(NowEntity, SensorEntity):
    """What the display shows right now: the player's song or station, or what the microphone hears."""

    _attr_name = "Showing"
    _attr_icon = "mdi:monitor"

    def __init__(self, entry) -> None:
        super().__init__(entry, "showing")

    def _view(self) -> tuple[str | None, dict[str, Any]]:
        p = self.dashboard.get("player") or {}
        if p.get("online") and p.get("source") and p.get("show"):
            return song(p.get("title"), p.get("artist")) if p.get("song_known") else (p.get("station") or p.get("title")), {
                "showing": "player", "player": p.get("name"), "source": p.get("source_name"),
                "station": p.get("station") or None, "title": p.get("title"), "artist": p.get("artist"),
                "album": p.get("album") or None, "year": p.get("year") or None,
                "recognised_from_stream": bool(p.get("recognised"))}
        a = self.dashboard.get("ambient") or {}
        if a.get("identified"):
            return song(a.get("title"), a.get("artist")), {"showing": "microphone", "title": a.get("title"),
                                                            "artist": a.get("artist"), "album": a.get("album") or None,
                                                            "year": a.get("year") or None}
        return "Listening", {"showing": "microphone"}

    @property
    def native_value(self) -> str | None:
        return self._view()[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._view()[1]


class HeardSensor(NowEntity, SensorEntity):
    """The song the microphone recognised (records, CDs), or nothing."""

    _attr_name = "Heard"
    _attr_icon = "mdi:microphone"

    def __init__(self, entry) -> None:
        super().__init__(entry, "heard")

    @property
    def native_value(self) -> str | None:
        a = self.dashboard.get("ambient") or {}
        return song(a.get("title"), a.get("artist")) if a.get("identified") else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        a = self.dashboard.get("ambient") or {}
        if not a.get("identified"):
            return {}
        return {"title": a.get("title"), "artist": a.get("artist"), "album": a.get("album") or None,
                "year": a.get("year") or None, "genre": a.get("genre") or None}
