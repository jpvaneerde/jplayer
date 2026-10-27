"""Base entities: one device per config entry, and helpers for reading a player's state."""
from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NOW_PORT, PLAYER_PORT, SOURCE_NAMES


class JPlayEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, entry, key: str) -> None:
        super().__init__(entry.runtime_data.coordinator)
        self.entry = entry
        self.api = entry.runtime_data.api
        self._attr_unique_id = f"{entry.unique_id}-{key}"


class PlayerEntity(JPlayEntity):
    """Something on a JPlay player."""

    def __init__(self, entry, key: str) -> None:
        super().__init__(entry, key)
        info = self.coordinator.info
        host, port = entry.data[CONF_HOST], entry.data.get(CONF_PORT, PLAYER_PORT)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name=info.get("name") or entry.title,
            manufacturer="JPlay",
            model="JPlay player",
            sw_version=info.get("version"),
            configuration_url=f"http://{host}:{port}",
        )

    # ---- reading the status
    @property
    def status(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("status") or {}

    @property
    def np(self) -> dict[str, Any]:
        return self.status.get("now_playing") or {}

    @property
    def extra(self) -> dict[str, Any]:
        return self.np.get("extra") or {}

    @property
    def active(self) -> str:
        return self.status.get("active") or ""

    def src(self, source_id: str) -> dict[str, Any]:
        return next((s for s in self.status.get("sources") or [] if s.get("id") == source_id), {})

    def source_name(self, source_id: str) -> str:
        return self.src(source_id).get("name") or SOURCE_NAMES.get(source_id, source_id.title())

    def details(self, source_id: str) -> dict[str, Any]:
        return self.src(source_id).get("details") or {}

    def art(self, url: str | None) -> str | None:
        """Cover addresses from the player are often paths on the player: make them full addresses."""
        if not url:
            return None
        return url if url.startswith(("http://", "https://")) else self.api.url(url if url.startswith("/") else f"/{url}")


class NowEntity(JPlayEntity):
    """Something on a JPlaying Now display."""

    def __init__(self, entry, key: str) -> None:
        super().__init__(entry, key)
        host, port = entry.data[CONF_HOST], entry.data.get(CONF_PORT, NOW_PORT)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name=entry.title,
            manufacturer="JPlay",
            model="JPlaying Now",
            configuration_url=f"http://{host}:{port}",
        )

    @property
    def dashboard(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("dashboard") or {}


def song(title: str | None, artist: str | None) -> str | None:
    title, artist = (title or "").strip(), (artist or "").strip()
    if title and artist:
        return f"{artist} – {title}"[:255]
    return (title or artist or None) and (title or artist)[:255]
