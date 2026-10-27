"""Switches: a player's Bluetooth pairing mode, and a JPlaying Now display's screen."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import KIND_PLAYER
from .entity import NowEntity, PlayerEntity
from .media_player import call


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    if entry.runtime_data.kind == KIND_PLAYER:
        async_add_entities([PairingSwitch(entry)])
    else:
        async_add_entities([ScreenSwitch(entry)])


class PairingSwitch(PlayerEntity, SwitchEntity):
    """Visible to phones as a Bluetooth speaker (a few minutes, as set on the player)."""

    _attr_name = "Bluetooth pairing"
    _attr_icon = "mdi:bluetooth-settings"

    def __init__(self, entry) -> None:
        super().__init__(entry, "bt_pairing")

    @property
    def available(self) -> bool:
        return super().available and bool(self.src("bluetooth").get("available"))

    @property
    def is_on(self) -> bool:
        return bool(self.details("bluetooth").get("discoverable"))

    async def _set(self, on: bool) -> None:
        await call(self.api.post("/api/v1/bluetooth/discoverable", {"on": on}))
        if not self.coordinator.ws_connected:
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class ScreenSwitch(NowEntity, SwitchEntity):
    """The display's screen. Off stays off until a touch, a new song or on."""

    _attr_name = "Screen"
    _attr_icon = "mdi:monitor"
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, entry) -> None:
        super().__init__(entry, "screen")

    @property
    def is_on(self) -> bool | None:
        screen = (self.coordinator.data or {}).get("screen")
        return screen.get("on") if screen else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        st = (self.coordinator.data or {}).get("screen_status") or {}
        return {"off_after_minutes": st.get("off_after_minutes"),
                "switched_off_by_hand": st.get("switched_off_by_hand"),
                "touch_devices": (st.get("touch") or {}).get("devices")}

    async def _set(self, on: bool) -> None:
        await call(self.api.post("/api/screen", {"on": on}))
        await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
