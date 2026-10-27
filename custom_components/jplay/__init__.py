"""JPlay for Home Assistant: JPlay players and JPlaying Now displays.

Each config entry is one device. Its `kind` says which of the two it is.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ApiClient, JPlayError
from .const import CONF_KIND, KIND_NOW, KIND_PLAYER, NOW_PORT, PLAYER_PORT
from .coordinator import NowCoordinator, PlayerCoordinator

PLATFORMS: dict[str, list[Platform]] = {
    KIND_PLAYER: [Platform.MEDIA_PLAYER, Platform.SELECT, Platform.SENSOR, Platform.BINARY_SENSOR,
                  Platform.BUTTON, Platform.SWITCH, Platform.UPDATE],
    KIND_NOW: [Platform.SWITCH, Platform.SELECT, Platform.SENSOR, Platform.UPDATE],
}


@dataclass
class JPlayData:
    kind: str
    api: ApiClient
    coordinator: Any


type JPlayConfigEntry = ConfigEntry[JPlayData]


async def async_setup_entry(hass: HomeAssistant, entry: JPlayConfigEntry) -> bool:
    kind = entry.data[CONF_KIND]
    session = async_get_clientsession(hass)
    if kind == KIND_PLAYER:
        api = ApiClient(session, f"http://{entry.data[CONF_HOST]}:{entry.data.get(CONF_PORT, PLAYER_PORT)}")
        try:
            info = await api.get("/api/v1/info")
        except JPlayError as err:
            raise ConfigEntryNotReady(str(err)) from err
        coordinator: Any = PlayerCoordinator(hass, entry, api, info)
    else:
        api = ApiClient(session, f"http://{entry.data[CONF_HOST]}:{entry.data.get(CONF_PORT, NOW_PORT)}")
        coordinator = NowCoordinator(hass, entry, api)

    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = JPlayData(kind, api, coordinator)
    if kind == KIND_PLAYER:
        entry.async_create_background_task(hass, coordinator.listen(), f"jplay events {entry.title}")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS[kind])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: JPlayConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS[entry.data[CONF_KIND]])
