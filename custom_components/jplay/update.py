"""Updates for players and JPlaying Now displays: the version, the newest release with its notes, and Install.

The Pi's own updater does the work (it restarts the app and goes back to the previous version if it doesn't
start); this entity asks for it and follows its progress, also for updates started on the Pi itself.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JPlayError
from .const import KIND_PLAYER
from .entity import NowEntity, PlayerEntity

INSTALL_TIMEOUT = 20 * 60
# How far an update is, by the updater's step
PERCENT = {"starting": 5, "fetching": 15, "applying": 35, "packages": 55, "restarting": 75, "verifying": 90,
           "rolling_back": 90}


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    if entry.runtime_data.kind == KIND_PLAYER:
        async_add_entities([PlayerUpdate(entry)])
    else:
        async_add_entities([NowUpdate(entry)])


class _Update:
    """What players and displays share; combined as (_Update, PlayerEntity/NowEntity, UpdateEntity) so the
    coordinator's availability and these properties win over UpdateEntity's defaults."""

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_name = None            # shown as the device: "Lounge" in Settings → Updates
    install_path = ""

    @property
    def st(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("update") or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.st.get("version"))

    @property
    def supported_features(self) -> UpdateEntityFeature:
        features = UpdateEntityFeature.RELEASE_NOTES | UpdateEntityFeature.PROGRESS
        if self.st.get("supported"):
            features |= UpdateEntityFeature.INSTALL
        return features

    @property
    def installed_version(self) -> str | None:
        return self.st.get("version")

    @property
    def latest_version(self) -> str | None:
        return self.st.get("latest") or self.st.get("version")

    def version_is_newer(self, latest_version: str, installed_version: str) -> bool:
        # The Pi knows best (releases or the newest commit, whichever it follows)
        return bool(self.st.get("available"))

    @property
    def in_progress(self) -> bool:
        return self.st.get("state") == "updating"

    @property
    def update_percentage(self) -> int | None:
        return PERCENT.get(self.st.get("step") or "") if self.in_progress else None

    @property
    def release_summary(self) -> str | None:
        if self.st.get("state") in ("failed", "rolled_back"):
            return self.st.get("message")
        notes = (self.st.get("notes") or "").strip()
        lines = [line for line in notes.splitlines() if line.strip() and not line.startswith("## ")]
        return " · ".join(lines)[:255] or None

    async def async_release_notes(self) -> str | None:
        return self.st.get("notes") or None

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        try:
            await self.api.post(self.install_path, {"target": self.st.get("target")} if self.st.get("target") else {})
        except JPlayError as err:
            raise HomeAssistantError(str(err)) from err
        # Follow the Pi's updater until it is done (the app restarts in between, so a refresh may fail)
        started = time.time()
        while time.time() - started < INSTALL_TIMEOUT:
            await asyncio.sleep(3)
            await self.coordinator.async_refresh()
            st = self.st
            if st.get("finished_at") and st["finished_at"] >= started - 5 and st.get("state") != "updating":
                if st.get("state") in ("failed", "rolled_back"):
                    raise HomeAssistantError(st.get("message") or "The update didn't work")
                return
        raise HomeAssistantError("The update takes unusually long; look at the device's settings page")


class PlayerUpdate(_Update, PlayerEntity, UpdateEntity):
    _attr_title = "JPlay"
    install_path = "/api/v1/update/install"

    def __init__(self, entry) -> None:
        PlayerEntity.__init__(self, entry, "update")


class NowUpdate(_Update, NowEntity, UpdateEntity):
    _attr_title = "JPlaying Now"
    install_path = "/api/update/install"

    def __init__(self, entry) -> None:
        NowEntity.__init__(self, entry, "update")
