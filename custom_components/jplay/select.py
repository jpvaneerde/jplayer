"""Pickers: a player's radio station and DAC, and which player a JPlaying Now display shows."""
from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import KIND_PLAYER
from .entity import NowEntity, PlayerEntity
from .media_player import call

AUTO = "Automatic"


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    if entry.runtime_data.kind == KIND_PLAYER:
        async_add_entities([StationSelect(entry), OutputSelect(entry)])
    else:
        async_add_entities([ShownPlayerSelect(entry)])


def unique_names(names: list[str]) -> list[str]:
    """Two favourites called the same get a number, so each can be picked."""
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n} ({seen[n]})")
    return out


class StationSelect(PlayerEntity, SelectEntity):
    """The radio station: shows the station playing, and picking one plays it."""

    _attr_name = "Radio station"
    _attr_icon = "mdi:radio"

    def __init__(self, entry) -> None:
        super().__init__(entry, "station")

    @property
    def _favs(self) -> list[dict[str, Any]]:
        return (self.coordinator.data or {}).get("favourites") or []

    @property
    def options(self) -> list[str]:
        return unique_names([f["name"] for f in self._favs])

    @property
    def available(self) -> bool:
        return super().available and bool(self._favs) and bool(self.src("radio").get("available"))

    @property
    def current_option(self) -> str | None:
        if self.active != "radio":
            return None
        station = self.extra.get("station") or ""
        url = self.extra.get("stream_url") or ""
        for name, fav in zip(self.options, self._favs):
            if (url and fav.get("url") == url) or fav["name"] == station:
                return name
        return None

    async def async_select_option(self, option: str) -> None:
        fav = dict(zip(self.options, self._favs))[option]
        await call(self.api.post("/api/v1/radio/play", {"uuid": fav["uuid"]}))


class OutputSelect(PlayerEntity, SelectEntity):
    """Which DAC the player plays to."""

    _attr_name = "Output"
    _attr_icon = "mdi:audio-input-rca"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, entry) -> None:
        super().__init__(entry, "output")

    @property
    def _choices(self) -> dict[str, str]:
        """Shown name -> device setting."""
        out = (self.coordinator.data or {}).get("output") or {}
        choices = {AUTO: "auto"}
        for card in out.get("cards") or []:
            choices[f"{card['name']} ({card['kind']})" if card.get("kind") else card["name"]] = card["device"]
        cur = out.get("current") or {}
        if cur.get("setting") and cur["setting"] not in choices.values():   # e.g. the chosen DAC is switched off
            name = cur.get("name") or cur["setting"]
            choices[f"{name} (off)" if cur.get("present") is False else name] = cur["setting"]
        return choices

    @property
    def options(self) -> list[str]:
        return list(self._choices)

    @property
    def current_option(self) -> str | None:
        setting = ((self.coordinator.data or {}).get("output") or {}).get("current", {}).get("setting")
        return next((name for name, dev in self._choices.items() if dev == setting), None)

    async def async_select_option(self, option: str) -> None:
        await call(self.api.put("/api/v1/output", {"device": self._choices[option]}))
        await self.coordinator.async_refresh()


class ShownPlayerSelect(NowEntity, SelectEntity):
    """The player a JPlaying Now display shows."""

    _attr_name = "Shown player"
    _attr_icon = "mdi:monitor-eye"

    def __init__(self, entry) -> None:
        super().__init__(entry, "view")

    @property
    def _players(self) -> list[dict[str, Any]]:
        return self.dashboard.get("players") or []

    @property
    def options(self) -> list[str]:
        return unique_names([p["name"] for p in self._players])

    @property
    def current_option(self) -> str | None:
        view = self.dashboard.get("view")
        return next((name for name, p in zip(self.options, self._players) if p["id"] == view), None)

    async def async_select_option(self, option: str) -> None:
        player = dict(zip(self.options, self._players))[option]
        await call(self.api.post("/api/view", {"player": player["id"]}))
        await self.coordinator.async_refresh()
