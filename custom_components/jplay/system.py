"""Raspberry Pi OS updates of a JPlay Pi: updates waiting, restart needed, install, restart.

One app per Pi speaks for it (the player when there is one: "primary" in its system status), so a Pi with a
player and a display shows these once.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .api import JPlayError
from .const import KIND_HUB, KIND_PLAYER
from .entity import HubEntity, NowEntity, PlayerEntity


def _base(entry):
    return {KIND_PLAYER: PlayerEntity, KIND_HUB: HubEntity}.get(entry.runtime_data.kind, NowEntity)


def _path(entry) -> str:
    return "/api/v1/system" if entry.runtime_data.kind == KIND_PLAYER else "/api/system"


def wanted(entry) -> bool:
    st = (entry.runtime_data.coordinator.data or {}).get("system") or {}
    return bool(st.get("supported") and st.get("primary"))


class _System:
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def sys(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("system") or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.sys)


def sensors(entry) -> list:
    base = _base(entry)

    class SystemUpdatesSensor(_System, base, SensorEntity):
        _attr_name = "System updates"
        _attr_icon = "mdi:package-up"
        _attr_state_class = SensorStateClass.MEASUREMENT

        def __init__(self) -> None:
            base.__init__(self, entry, "system-updates")

        @property
        def native_value(self) -> int:
            return int(self.sys.get("updates") or 0)

        @property
        def extra_state_attributes(self) -> dict[str, Any]:
            s = self.sys
            return {"os": s.get("os"), "kernel": s.get("kernel"), "packages": s.get("packages") or [],
                    "state": s.get("state"), "automatic": s.get("auto") == "night", "checked_at": s.get("checked_at")}

    return [SystemUpdatesSensor()]


def binary_sensors(entry) -> list:
    base = _base(entry)

    class RestartNeeded(_System, base, BinarySensorEntity):
        _attr_name = "Restart needed"
        _attr_device_class = BinarySensorDeviceClass.UPDATE

        def __init__(self) -> None:
            base.__init__(self, entry, "system-restart-needed")

        @property
        def is_on(self) -> bool:
            return bool(self.sys.get("reboot_needed"))

    return [RestartNeeded()]


def buttons(entry) -> list:
    base = _base(entry)
    path = _path(entry)

    class SystemButton(_System, base, ButtonEntity):
        def __init__(self, key: str, name: str, icon: str, action: str, body: dict | None,
                     device_class: ButtonDeviceClass | None = None) -> None:
            base.__init__(self, entry, key)
            self._attr_name, self._attr_icon, self._attr_device_class = name, icon, device_class
            self._action, self._body = action, body

        async def async_press(self) -> None:
            try:
                await self.api.post(f"{path}/{self._action}", self._body or {})
            except JPlayError as err:
                raise HomeAssistantError(str(err)) from err
            await self.coordinator.async_request_refresh()

    return [
        SystemButton("system-install", "Install system updates", "mdi:package-down", "install", {"reboot": True}),
        SystemButton("system-restart", "Restart Pi", "mdi:restart", "reboot", None, ButtonDeviceClass.RESTART),
    ]
