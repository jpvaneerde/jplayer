"""The JPlay hub in Home Assistant: per room what plays now (and whether its display reports), today's totals,
and the last record put on. Rooms that appear later (a new display) get their entities by themselves."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .entity import HubEntity, song

SOURCES = {"vinyl": "Record", "cd": "CD", "cassette": "Cassette", "radio": "Radio", "plex": "Plex",
           "spotify": "Spotify", "bluetooth": "Bluetooth"}


def add_room_entities(entry, async_add_entities: AddEntitiesCallback, make: Callable[[Any, str], list]) -> None:
    """Entities for every room, now and whenever a new room shows up on the hub."""
    coordinator = entry.runtime_data.coordinator
    known: set[str] = set()

    @callback
    def check() -> None:
        new = [r["room"] for r in (coordinator.data or {}).get("rooms") or [] if r.get("room") and r["room"] not in known]
        if new:
            known.update(new)
            async_add_entities([e for name in new for e in make(entry, name)])

    check()
    entry.async_on_unload(coordinator.async_add_listener(check))


# ==================================================================== per room
class RoomNowPlaying(HubEntity, SensorEntity):
    """'Artist – Title' of what plays in the room, or the station, or Quiet. The cover is the entity picture."""

    _attr_icon = "mdi:music-circle"

    def __init__(self, entry, room: str) -> None:
        super().__init__(entry, f"room-{slugify(room)}-now")
        self.room_name = room
        self._attr_name = f"{room} now playing"

    @property
    def r(self) -> dict[str, Any]:
        return self.room(self.room_name) or {}

    @property
    def native_value(self) -> str:
        play, session = self.r.get("play"), self.r.get("session")
        if play:
            return song(play.get("title"), play.get("artist")) or "Playing"
        if session:
            return (session.get("station") or SOURCES.get(session.get("mode"), "Playing"))[:255]
        return "Quiet"

    @property
    def entity_picture(self) -> str | None:
        return self.cover((self.r.get("play") or {}).get("cover"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        play, session = self.r.get("play") or {}, self.r.get("session") or {}
        record = play.get("record") or {}
        kind = play.get("kind")
        return {
            "title": play.get("title"), "artist": play.get("artist"), "album": play.get("album"),
            "kind": {"physical": "record", "streamed": "stream", "heard": "heard"}.get(kind) if kind else None,
            "source": SOURCES.get(play.get("detail") or session.get("mode") or "", play.get("detail") or session.get("mode")),
            "station": play.get("station") or session.get("station") or None,
            "record": f"{record.get('artist')} – {record.get('title')}" if record else None,
            "display_online": bool(self.r.get("online")),
        }


class RoomDisplayOnline(HubEntity, BinarySensorEntity):
    """Does the room's display report to the hub?"""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, entry, room: str) -> None:
        super().__init__(entry, f"room-{slugify(room)}-online")
        self.room_name = room
        self._attr_name = f"{room} display"

    @property
    def is_on(self) -> bool:
        return bool((self.room(self.room_name) or {}).get("online"))


# ==================================================================== the house
class TodaySensor(HubEntity, SensorEntity):
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, entry, key: str, name: str, icon: str, value: Callable[[dict], Any], unit: str | None = None,
                 precision: int | None = None) -> None:
        super().__init__(entry, key)
        self._attr_name, self._attr_icon, self._value = name, icon, value
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = precision

    @property
    def native_value(self) -> Any:
        today = (self.coordinator.data or {}).get("today") or {}
        return self._value(today) if today else None

    @property
    def last_reset(self):
        return dt_util.start_of_local_day()


def today_sensors(entry) -> list:
    return [
        TodaySensor(entry, "today-records", "Records put on today", "mdi:album", lambda t: t["physical"]["spins"]),
        TodaySensor(entry, "today-songs", "Songs played today", "mdi:music-note-plus",
                    lambda t: t["physical"]["tracks"] + t["streamed"]["tracks"] + t["heard"]["tracks"]),
        TodaySensor(entry, "today-listening", "Radio and streams today", "mdi:radio",
                    lambda t: round(t.get("listening_hours") or 0, 2), UnitOfTime.HOURS, 1),
        TodaySensor(entry, "today-vinyl", "Vinyl today", "mdi:record-player",
                    lambda t: round(t["physical"].get("vinyl_hours") or 0, 2), UnitOfTime.HOURS, 1),
    ]


class LastRecordSensor(HubEntity, SensorEntity):
    """The last record (or CD) put on: 'Artist – Title', with its cover."""

    _attr_name = "Last record put on"
    _attr_icon = "mdi:record-player"

    def __init__(self, entry) -> None:
        super().__init__(entry, "last-record")

    @property
    def spin(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("spin") or {}

    @property
    def native_value(self) -> str | None:
        rec = self.spin.get("record") or {}
        return song(rec.get("title"), rec.get("artist"))

    @property
    def entity_picture(self) -> str | None:
        return self.cover((self.spin.get("record") or {}).get("cover"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        s = self.spin
        if not s:
            return {}
        return {"room": s.get("room"), "medium": SOURCES.get(s.get("medium"), s.get("medium")),
                "tracks": s.get("tracks"), "year": (s.get("record") or {}).get("year"),
                "started": dt_util.utc_from_timestamp(s["started_at"] / 1000).isoformat() if s.get("started_at") else None}
