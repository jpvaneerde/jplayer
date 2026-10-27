"""Buttons on a player: eject the CD, go to standby."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PlayerEntity
from .media_player import call


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([
        PlayerButton(entry, "eject", "Eject CD", "mdi:eject", "/api/v1/cd/eject", needs="cd"),
        PlayerButton(entry, "standby", "Standby", "mdi:power-sleep", "/api/v1/standby"),
    ])


class PlayerButton(PlayerEntity, ButtonEntity):
    def __init__(self, entry, key: str, name: str, icon: str, path: str, needs: str | None = None) -> None:
        super().__init__(entry, key)
        self._attr_name, self._attr_icon = name, icon
        self._path, self._needs = path, needs

    @property
    def available(self) -> bool:
        return super().available and (not self._needs or bool(self.src(self._needs).get("available")))

    async def async_press(self) -> None:
        await call(self.api.post(self._path))
