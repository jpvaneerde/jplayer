"""Keeping each device's state up to date.

- A JPlay player pushes its state over a WebSocket (/api/v1/events) the moment anything changes; every 30 s the
  coordinator also reads the radio favourites and the DACs, and the status again in case the WebSocket is down.
- JPlaying Now is asked every 5 seconds.
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import ApiClient, JPlayError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

POSITION_DRIFT = 3.0   # seconds: a position update further off than this (a seek) is passed on


def _without_positions(status: dict[str, Any]) -> dict[str, Any]:
    s = copy.deepcopy(status)
    (s.get("now_playing") or {}).pop("position", None)
    for src in s.get("sources") or []:
        (src.get("now_playing") or {}).pop("position", None)
    return s


class PlayerCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """data = {"status": /api/v1/status, "favourites": [...], "output": /api/v1/output or None}"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: ApiClient, info: dict[str, Any]) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {info.get('name')}",
                         update_interval=timedelta(seconds=30))
        self.api = api
        self.info = info
        self.ws_connected = False
        self.position_updated_at = dt_util.utcnow()
        self._pos: tuple[float | None, float] = (None, time.monotonic())

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            status = await self.api.get("/api/v1/status")
        except JPlayError as err:
            raise UpdateFailed(str(err)) from err
        favourites, output, update = await asyncio.gather(
            self.api.get("/api/v1/radio/favourites"), self.api.get("/api/v1/output"), self.api.get("/api/v1/update"),
            return_exceptions=True,
        )
        self._note_position(status)
        return {
            "status": status,
            "favourites": favourites if isinstance(favourites, list) else (self.data or {}).get("favourites", []),
            "output": output if isinstance(output, dict) else (self.data or {}).get("output"),
            "update": update if isinstance(update, dict) else None,   # None: a player without the update API
        }

    # ---------------------------------------------------------------- live updates
    def _note_position(self, status: dict[str, Any]) -> None:
        self._pos = ((status.get("now_playing") or {}).get("position"), time.monotonic())
        self.position_updated_at = dt_util.utcnow()

    def _significant(self, old: dict[str, Any], new: dict[str, Any]) -> bool:
        """Anything but the playing position changed, or the position jumped (a seek)."""
        if _without_positions(old) != _without_positions(new):
            return True
        pos = (new.get("now_playing") or {}).get("position")
        last, at = self._pos
        if pos is None or last is None:
            return pos != last
        playing = (new.get("now_playing") or {}).get("state") == "playing"
        expected = last + (time.monotonic() - at if playing else 0)
        return abs(pos - expected) > POSITION_DRIFT

    def _push(self, status: dict[str, Any]) -> None:
        if self.data is None:
            return
        new = {**self.data, "status": status}
        if not self._significant(self.data["status"], status):
            # Only the position moved on as expected: keep it without waking every entity each second.
            self.data = new
            return
        self._note_position(status)
        self.async_set_updated_data(new)

    async def listen(self) -> None:
        """Follow the player's WebSocket for as long as Home Assistant runs (reconnecting when needed)."""
        session = self.api.session
        url = self.api.url("/api/v1/events").replace("http", "ws", 1)
        delay = 2
        while True:
            try:
                async with session.ws_connect(url, heartbeat=30, timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    self.ws_connected = True
                    delay = 2
                    _LOGGER.debug("Connected to %s", url)
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = msg.json()
                            if data.get("type") == "state" and isinstance(data.get("data"), dict):
                                self._push(data["data"])
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as err:  # unreachable, restarting...
                _LOGGER.debug("Events from %s: %s", url, err)
            if self.ws_connected:
                self.ws_connected = False
                # Let entities go unavailable quickly if the player is really gone
                self.hass.async_create_task(self.async_request_refresh())
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


class NowCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """data = {"dashboard": /api/dashboard, "screen": /api/screen, "screen_status": /api/screen-status}"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: ApiClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} {entry.title}",
                         update_interval=timedelta(seconds=5))
        self.api = api

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            dashboard, screen_status = await asyncio.gather(self.api.get("/api/dashboard"), self.api.get("/api/screen-status"))
        except JPlayError as err:
            raise UpdateFailed(str(err)) from err
        try:
            update = await self.api.get("/api/update")
        except JPlayError:
            update = None                  # a display without the update API
        dashboard.pop("config", None)  # settings (and their tokens) aren't needed here
        return {"dashboard": dashboard, "screen": dashboard.get("screen"), "screen_status": screen_status, "update": update}
