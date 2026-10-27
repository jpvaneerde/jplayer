"""Yes/no states of a player: playing, DAC switched on, CD in the drive, a phone on Bluetooth."""
from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import PlayerEntity


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([
        PlayerBinary(entry, "playing", "Playing", "mdi:play-circle", BinarySensorDeviceClass.RUNNING,
                     lambda e: bool(e.active) and e.np.get("state") in ("playing", "loading")),
        PlayerBinary(entry, "dac", "DAC", "mdi:amplifier", BinarySensorDeviceClass.CONNECTIVITY,
                     lambda e: bool((e.status.get("output") or {}).get("present")), EntityCategory.DIAGNOSTIC),
        PlayerBinary(entry, "cd_inserted", "CD inserted", "mdi:disc-player", None,
                     lambda e: (e.details("cd").get("status") or "empty") not in ("empty", "error"),
                     needs="cd"),
        PlayerBinary(entry, "bt_connected", "Bluetooth connected", "mdi:bluetooth-connect",
                     BinarySensorDeviceClass.CONNECTIVITY,
                     lambda e: any(d.get("connected") for d in e.details("bluetooth").get("devices") or []),
                     needs="bluetooth"),
    ])


class PlayerBinary(PlayerEntity, BinarySensorEntity):
    def __init__(self, entry, key: str, name: str, icon: str, device_class, value: Callable[[PlayerEntity], bool],
                 category: EntityCategory | None = None, needs: str | None = None) -> None:
        super().__init__(entry, key)
        self._attr_name, self._attr_icon, self._attr_device_class = name, icon, device_class
        self._attr_entity_category = category
        self._value, self._needs = value, needs

    @property
    def available(self) -> bool:
        return super().available and (not self._needs or bool(self.src(self._needs).get("available")))

    @property
    def is_on(self) -> bool:
        return self._value(self)
