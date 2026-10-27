"""Internet radio: favourites, Radio Browser search, ICY now-playing titles."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ..core.models import Capabilities, PlayState
from .base import SourceError
from .engine_source import EngineSource

log = logging.getLogger(__name__)

FAV_KEY = "radio.favourites"
LAST_KEY = "radio.last_station"


def normalize_station(raw: dict[str, Any]) -> dict[str, Any]:
    """Radio Browser station (or one we normalised before) -> the compact shape we store.

    Must accept its own output too: the UI sends back stations it got from us.
    """
    raw_tags = raw.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = raw_tags.split(",")
    tags = [str(t).strip() for t in raw_tags if str(t).strip()]
    return {
        "uuid": raw.get("stationuuid") or raw.get("uuid") or raw.get("url", ""),
        "name": (raw.get("name") or "").strip() or "Unnamed station",
        "url": raw.get("url_resolved") or raw.get("url") or "",
        "favicon": raw.get("favicon") or "",
        "country": raw.get("country") or "",
        "countrycode": raw.get("countrycode") or "",
        "codec": raw.get("codec") or "",
        "bitrate": raw.get("bitrate") or 0,
        "tags": tags[:4],
        "homepage": raw.get("homepage") or "",
    }


def split_icy_title(title: str) -> tuple[str, str]:
    """'Artist - Title' -> (artist, title). Anything else -> ('', title)."""
    title = (title or "").strip()
    for sep in (" - ", " – ", " — "):
        if sep in title:
            artist, _, song = title.partition(sep)
            if artist.strip() and song.strip():
                return artist.strip(), song.strip()
    return "", title


class RadioSource(EngineSource):
    id = "radio"
    name = "Radio"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.base_url = self.cfg.get("radio_browser_url", "https://de1.api.radio-browser.info").rstrip("/")
        self.station: dict[str, Any] | None = ctx.store.get(LAST_KEY)
        self._want_playing = False
        self._user_paused = False
        self._retries = 0
        self._retry_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        ok, reason = self.engine_ready()
        self.set_available(ok, reason)
        if self.station:
            self._show_station(self.station, PlayState.STOPPED)

    def capabilities(self) -> Capabilities:
        many = len(self.favourites()) > 1
        has = self.station is not None
        return Capabilities(play=has, pause=has, stop=has, next=many, previous=many, seek=False)

    def details(self) -> dict[str, Any]:
        return {"station": self.station}

    # ------------------------------------------------------------ favourites
    def favourites(self) -> list[dict[str, Any]]:
        return self.ctx.store.get(FAV_KEY, [])

    def add_favourite(self, station: dict[str, Any]) -> list[dict[str, Any]]:
        station = normalize_station(station)
        if not station["url"]:
            raise SourceError("Station has no stream URL")
        favs = [f for f in self.favourites() if f["uuid"] != station["uuid"]]
        favs.append(station)
        self.ctx.store.set(FAV_KEY, favs)
        self.ctx.manager.notify(True)
        return favs

    def remove_favourite(self, uuid: str) -> list[dict[str, Any]]:
        favs = [f for f in self.favourites() if f["uuid"] != uuid]
        self.ctx.store.set(FAV_KEY, favs)
        self.ctx.manager.notify(True)
        return favs

    def reorder_favourites(self, uuids: list[str]) -> list[dict[str, Any]]:
        by_id = {f["uuid"]: f for f in self.favourites()}
        ordered = [by_id.pop(u) for u in uuids if u in by_id]
        ordered += list(by_id.values())
        self.ctx.store.set(FAV_KEY, ordered)
        return ordered

    # ------------------------------------------------------------ Radio Browser
    async def search(self, name: str = "", countrycode: str = "", tag: str = "", limit: int = 40) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": max(1, min(limit, 100)),
            "hidebroken": "true",
            "order": "clickcount",
            "reverse": "true",
        }
        if name:
            params["name"] = name
        if countrycode:
            params["countrycode"] = countrycode.upper()
        if tag:
            params["tag"] = tag
        try:
            resp = await self.ctx.http.get(f"{self.base_url}/json/stations/search", params=params, timeout=15)
            resp.raise_for_status()
        except Exception as exc:
            raise SourceError(f"Radio Browser search failed: {exc}", 502) from exc
        return [normalize_station(s) for s in resp.json() if s.get("url_resolved") or s.get("url")]

    async def _count_click(self, uuid: str) -> None:
        # Radio Browser asks clients to report plays; it keeps their popularity ranking useful.
        with contextlib.suppress(Exception):
            await self.ctx.http.get(f"{self.base_url}/json/url/{uuid}", timeout=10)

    # ------------------------------------------------------------ playback
    def _show_station(self, station: dict[str, Any], state: PlayState) -> None:
        self.reset_now_playing(
            state=state,
            title=station["name"],
            album=station["name"],
            art_url=station.get("favicon", ""),
            extra={
                "station": station["name"],
                "station_uuid": station["uuid"],
                # The stream itself: lets a display (JPlaying Now) recognise songs the station doesn't name.
                "stream_url": station["url"],
                "codec": station.get("codec", ""),
                "bitrate": station.get("bitrate", 0),
            },
        )

    async def play_station(self, station: dict[str, Any]) -> None:
        station = normalize_station(station)
        if not station["url"]:
            raise SourceError("Station has no stream URL")
        self._cancel_retry()
        await self.claim()
        self.station = station
        self.ctx.store.set(LAST_KEY, station)
        self._want_playing = True
        self._user_paused = False
        self._retries = 0
        self._show_station(station, PlayState.LOADING)
        await self.engine.command("set_property", "pause", False)
        await self.engine.command("loadfile", station["url"], "replace")
        asyncio.create_task(self._count_click(station["uuid"]))

    async def play(self) -> None:
        if self.station is None:
            favs = self.favourites()
            if not favs:
                raise SourceError("No station selected. Add a favourite or search for one.")
            self.station = favs[0]
        await self.play_station(self.station)

    async def pause(self) -> None:
        # Pausing a live stream only builds a backlog; stop it and resume live later.
        self._want_playing = False
        self._user_paused = True
        self._cancel_retry()
        if self.owns_engine:
            await self.engine.command("stop")
        self.update(state=PlayState.PAUSED, position=None)

    async def toggle(self) -> None:
        if self.np.state in (PlayState.PLAYING, PlayState.LOADING):
            await self.pause()
        else:
            await self.play()

    async def stop(self) -> None:
        self._want_playing = False
        self._user_paused = False
        self._cancel_retry()
        await super().stop()

    async def _step(self, delta: int) -> None:
        favs = self.favourites()
        if not favs:
            raise SourceError("No favourites to step through")
        uuids = [f["uuid"] for f in favs]
        current = uuids.index(self.station["uuid"]) if self.station and self.station["uuid"] in uuids else -1
        await self.play_station(favs[(current + delta) % len(favs)])

    async def next(self) -> None:
        await self._step(1)

    async def previous(self) -> None:
        await self._step(-1)

    async def deactivate(self) -> None:
        self._want_playing = False
        self._user_paused = False
        self._cancel_retry()
        await super().deactivate()

    # ------------------------------------------------------------ mpv callbacks
    async def on_metadata(self, metadata: dict[str, Any]) -> None:
        if not self.station:
            return
        icy = metadata.get("icy-title") or metadata.get("title") or ""
        artist, title = split_icy_title(icy)
        extra = dict(self.np.extra)
        if metadata.get("icy-genre"):
            extra["genre"] = metadata["icy-genre"]
        if icy:
            self.update(title=title, artist=artist, extra=extra)
        else:
            self.update(title=self.station["name"], artist="", extra=extra)

    async def on_mpv_event(self, event: dict[str, Any]) -> None:
        await super().on_mpv_event(event)
        if event.get("event") == "file-loaded":
            self._retries = 0

    async def on_idle(self) -> None:
        if self._user_paused:
            self.update(state=PlayState.PAUSED)
            return
        # The stream dropped. Reconnect with back-off, like a real tuner would.
        if not (self._want_playing and self.station and self.active):
            return
        if self._retries >= 6:
            self._want_playing = False
            self.update(extra={**self.np.extra, "error": "Stream keeps dropping; stopped retrying"})
            return
        self._retries += 1
        delay = min(30, 2 ** self._retries)
        log.info("Radio stream ended; reconnecting in %ss (attempt %s)", delay, self._retries)
        self.update(state=PlayState.LOADING)
        self._retry_task = asyncio.create_task(self._reconnect(delay))

    async def _reconnect(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self._want_playing and self.station and self.owns_engine:
            await self.engine.command("loadfile", self.station["url"], "replace")

    def _cancel_retry(self) -> None:
        if self._retry_task and not self._retry_task.done():
            self._retry_task.cancel()
        self._retry_task = None
