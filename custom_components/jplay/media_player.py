"""A JPlay player as a media player: what plays, play/pause/skip, volume, source, and the media browser
(radio favourites, the CD, Plex playlists)."""
from __future__ import annotations

from typing import Any
from homeassistant.components import media_source
from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature as F,
    MediaPlayerState,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import JPlayError
from .entity import PlayerEntity

STATES = {
    "playing": MediaPlayerState.PLAYING,
    "loading": MediaPlayerState.BUFFERING,
    "paused": MediaPlayerState.PAUSED,
    "stopped": MediaPlayerState.IDLE,
}
CAPABILITY_FEATURES = {"play": F.PLAY, "pause": F.PAUSE, "stop": F.STOP, "next": F.NEXT_TRACK,
                       "previous": F.PREVIOUS_TRACK, "seek": F.SEEK}
# Sources that start playing by themselves when picked; Spotify and Bluetooth wait for the phone.
STARTS_PLAYING = ("radio", "cd", "plex")


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddEntitiesCallback) -> None:
    async_add_entities([JPlayMediaPlayer(entry)])


async def call(coro) -> Any:
    try:
        return await coro
    except JPlayError as err:
        raise HomeAssistantError(str(err)) from err


class JPlayMediaPlayer(PlayerEntity, MediaPlayerEntity):
    _attr_name = None   # the device's own name
    _attr_device_class = MediaPlayerDeviceClass.SPEAKER
    _attr_media_content_type = MediaType.MUSIC

    def __init__(self, entry) -> None:
        super().__init__(entry, "media_player")
        self._last_source = "radio"

    # ---------------------------------------------------------------- state
    @property
    def state(self) -> MediaPlayerState:
        if not self.active:
            return MediaPlayerState.OFF
        return STATES.get(self.np.get("state"), MediaPlayerState.ON)

    @property
    def supported_features(self) -> F:
        features = F.SELECT_SOURCE | F.TURN_ON | F.TURN_OFF | F.PLAY_MEDIA | F.BROWSE_MEDIA
        caps = self.status.get("capabilities") or {}
        for cap, feature in CAPABILITY_FEATURES.items():
            if caps.get(cap):
                features |= feature
        if not self.active:
            features |= F.PLAY          # play = switch on
        if self.status.get("volume_available"):
            features |= F.VOLUME_SET | F.VOLUME_STEP
        return features

    @property
    def source_list(self) -> list[str]:
        return [s["name"] for s in self.status.get("sources") or [] if s.get("available")]

    @property
    def source(self) -> str | None:
        return self.source_name(self.active) if self.active else None

    @property
    def app_name(self) -> str | None:
        return self.source

    @property
    def volume_level(self) -> float | None:
        v = self.status.get("volume")
        return v / 100 if self.status.get("volume_available") and v is not None else None

    @property
    def media_title(self) -> str | None:
        return self.np.get("title") or None if self.active else None

    @property
    def media_artist(self) -> str | None:
        return self.np.get("artist") or None if self.active else None

    @property
    def media_album_name(self) -> str | None:
        return self.np.get("album") or None if self.active else None

    @property
    def media_channel(self) -> str | None:
        return self.extra.get("station") or None if self.active == "radio" else None

    @property
    def media_track(self) -> int | None:
        return self.np.get("track_number") if self.active else None

    @property
    def media_duration(self) -> int | None:
        d = self.np.get("duration")
        return round(d) if d and self.active else None

    @property
    def media_position(self) -> int | None:
        p = self.np.get("position")
        return round(p) if p is not None and self.media_duration else None

    @property
    def media_position_updated_at(self):
        return self.coordinator.position_updated_at if self.media_position is not None else None

    @property
    def media_image_url(self) -> str | None:
        return self.art(self.np.get("art_url")) if self.active else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source_id": self.active or None}

    def _handle_coordinator_update(self) -> None:
        if self.active:
            self._last_source = self.active
        super()._handle_coordinator_update()

    async def _after(self) -> None:
        if not self.coordinator.ws_connected:
            await self.coordinator.async_request_refresh()

    # ---------------------------------------------------------------- control
    async def _transport(self, command: str) -> None:
        await call(self.api.post(f"/api/v1/player/{command}"))
        await self._after()

    async def async_media_play(self) -> None:
        if not self.active:
            await self.async_turn_on()
            return
        await self._transport("play")

    async def async_media_pause(self) -> None:
        await self._transport("pause")

    async def async_media_stop(self) -> None:
        await self._transport("stop")

    async def async_media_next_track(self) -> None:
        await self._transport("next")

    async def async_media_previous_track(self) -> None:
        await self._transport("previous")

    async def async_media_seek(self, position: float) -> None:
        await call(self.api.post("/api/v1/player/seek", {"position": max(0.0, position)}))
        await self._after()

    async def async_set_volume_level(self, volume: float) -> None:
        await call(self.api.put("/api/v1/volume", {"volume": round(volume * 100)}))
        await self._after()

    async def async_turn_off(self) -> None:
        await call(self.api.post("/api/v1/standby"))
        await self._after()

    async def async_turn_on(self) -> None:
        await self._activate(self._last_source)

    async def async_select_source(self, source: str) -> None:
        sid = next((s["id"] for s in self.status.get("sources") or [] if s["name"] == source or s["id"] == source), None)
        if sid is None:
            raise HomeAssistantError(f"{self.name} has no source called {source}")
        await self._activate(sid)

    async def _activate(self, source_id: str) -> None:
        await call(self.api.post(f"/api/v1/sources/{source_id}/activate"))
        if source_id in STARTS_PLAYING:
            try:
                await self.api.post("/api/v1/player/play")
            except JPlayError:
                pass  # no disc, no station, empty Plex queue: the source is picked, it just waits
        await self._after()

    # ---------------------------------------------------------------- playing something specific
    async def async_play_media(self, media_type: str, media_id: str, **kwargs: Any) -> None:
        """media_id: radio:<station uuid or name>, cd:<track>, plex:<key>, an http(s) stream, or a media source."""
        if media_source.is_media_source_id(media_id):
            item = await media_source.async_resolve_media(self.hass, media_id, self.entity_id)
            media_id = async_process_play_media_url(self.hass, item.url)
        kind, _, value = media_id.partition(":")
        if kind == "radio":
            favs = (self.coordinator.data or {}).get("favourites") or []
            fav = next((f for f in favs if f["uuid"] == value), None) or next(
                (f for f in favs if f["name"].casefold() == value.strip().casefold()), None)
            if fav is None:
                raise HomeAssistantError(f"'{value}' is not one of {self.name}'s radio favourites")
            await call(self.api.post("/api/v1/radio/play", {"uuid": fav["uuid"]}))
        elif kind == "cd":
            await call(self.api.post("/api/v1/cd/play", {"track": int(value or 1)}))
        elif kind == "plex":
            await call(self.api.post("/api/v1/plex/play", {"key": value}))
        elif kind in ("http", "https"):
            name = kwargs.get("extra", {}).get("title") or "Home Assistant"
            await call(self.api.post("/api/v1/radio/play", {"station": {"url": media_id, "name": name}}))
        else:
            raise HomeAssistantError(f"Can't play '{media_id}': use radio:<station>, cd:<track> or plex:<key>")
        await self._after()

    async def async_browse_media(self, media_content_type: str | None = None,
                                 media_content_id: str | None = None) -> BrowseMedia:
        if media_content_id and media_source.is_media_source_id(media_content_id):
            return await media_source.async_browse_media(self.hass, media_content_id)
        cid = media_content_id or "root"
        if cid == "radio":
            favs = (self.coordinator.data or {}).get("favourites") or []
            return self._folder("radio", "Radio favourites",
                                [self._item(f"radio:{f['uuid']}", f["name"], f.get("favicon"), MediaClass.CHANNEL) for f in favs])
        if cid == "cd":
            disc = self.details("cd").get("disc") or {}
            tracks = [self._item(f"cd:{t['number']}", f"{t['number']}. {t.get('title') or 'Track'}", disc.get("art_url"),
                                 MediaClass.TRACK) for t in disc.get("tracks") or []]
            return self._folder("cd", disc.get("album") or "CD", tracks)
        if cid == "plex":
            playlists = await call(self.api.get("/api/v1/plex/playlists"))
            return self._folder("plex", "Plex playlists",
                                [self._item(f"plex:{p['key']}", p["title"], p.get("thumb"), MediaClass.PLAYLIST) for p in playlists])
        children = []
        if self.src("radio").get("available"):
            children.append(self._folder("radio", "Radio favourites", None))
        if self.details("cd").get("disc"):
            children.append(self._folder("cd", self.details("cd")["disc"].get("album") or "CD", None))
        if self.src("plex").get("available"):
            children.append(self._folder("plex", "Plex playlists", None))
        try:  # Home Assistant's own media (local files, text-to-speech...), when that is set up
            children.append(await media_source.async_browse_media(
                self.hass, None, content_filter=lambda item: item.media_content_type.startswith("audio/")))
        except BrowseError:
            pass
        return self._folder("root", self.name or "JPlay", children)

    def _folder(self, cid: str, title: str, children: list[BrowseMedia] | None) -> BrowseMedia:
        return BrowseMedia(media_class=MediaClass.DIRECTORY, media_content_id=cid, media_content_type="jplay",
                           title=title, can_play=False, can_expand=True, children=children,
                           children_media_class=MediaClass.MUSIC)

    def _item(self, cid: str, title: str, thumb: str | None, cls: str) -> BrowseMedia:
        url = self.art(thumb)
        return BrowseMedia(media_class=cls, media_content_id=cid, media_content_type=MediaType.MUSIC, title=title,
                           can_play=True, can_expand=False,
                           thumbnail=self.get_browse_image_url("jplay", cid, url) if url else None)

    async def async_get_browse_image(self, media_content_type: str, media_content_id: str,
                                     media_image_id: str | None = None) -> tuple[bytes | None, str | None]:
        """Covers and logos go through Home Assistant (the player is http-only, on the home network)."""
        if not media_image_id or not media_image_id.startswith((self.api.base + "/", "https://")):
            return None, None
        return await self._async_fetch_image(media_image_id)
