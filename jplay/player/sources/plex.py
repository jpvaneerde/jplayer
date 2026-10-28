"""Plex: browse a music library through the Plex API and play the original files.

Tracks are streamed direct (no transcoding), so FLAC stays FLAC all the way to the DAC.
python-plexapi is synchronous, so every call runs in a worker thread.

The server's address and token come from the player's web page (Plex → Connect, kept in the state file), or from
plex.url / plex.token in the config file. The web page can also sign in with a Plex account (as a TV app does:
approve on plex.tv, and the player gets a token of its own) and then pick one of the account's servers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..core.models import Capabilities, PlayState
from .base import SourceError
from .engine_source import EngineSource

log = logging.getLogger(__name__)

MUSIC_TYPES = ("artist", "album", "track")
SETTINGS_KEY = "plex.connection"     # {"url", "token"} saved from the web page; wins over the config file
CLIENT_ID_KEY = "plex.client_id"
PIN_TIMEOUT = 10 * 60               # seconds a sign-in may take
PLEX_TYPE = {"artist": 8, "album": 9, "track": 10}


def art_url(path: str | None, size: int = 300) -> str:
    """Browser-safe artwork URL. The Plex token never leaves the Pi."""
    if not path:
        return ""
    return f"/api/v1/plex/art?path={quote(path, safe='')}&size={size}"


def _item(obj: Any) -> dict[str, Any]:
    """Any Plex music object -> compact dict for the UI."""
    kind = getattr(obj, "type", "")
    out: dict[str, Any] = {"key": str(obj.ratingKey), "type": kind, "title": obj.title or ""}
    if kind == "artist":
        out["subtitle"] = ""
        out["thumb"] = art_url(obj.thumb)
    elif kind == "album":
        out["subtitle"] = obj.parentTitle or ""
        out["year"] = obj.year
        out["thumb"] = art_url(obj.thumb)
    elif kind == "track":
        out["subtitle"] = f"{obj.originalTitle or obj.grandparentTitle or ''} · {obj.parentTitle or ''}".strip(" ·")
        out["duration"] = round((obj.duration or 0) / 1000)
        out["index"] = obj.index
        out["thumb"] = art_url(obj.parentThumb or obj.thumb)
    elif kind == "playlist":
        out["subtitle"] = f"{obj.leafCount or 0} tracks"
        out["duration"] = round((obj.duration or 0) / 1000)
        out["thumb"] = art_url(getattr(obj, "composite", None) or obj.thumb)
    return out


def _artist_albums(srv: Any, artist: Any) -> list[Any]:
    """All of an artist's releases. The artist's own list (/children) can leave out singles and EPs (Plex files
    them under "Singles & EPs"), so the library is also asked for every album by the artist, and albums that only
    have tracks by the artist (on someone else's record) come last."""
    rid = artist.ratingKey
    seen: dict[str, Any] = {}

    def add(items):
        for a in items:
            if getattr(a, "type", "") == "album" and str(a.ratingKey) not in seen:
                seen[str(a.ratingKey)] = a

    for path in (f"/library/metadata/{rid}/children",
                 f"/library/sections/{artist.librarySectionID}/all?type=9&artist.id={rid}"):
        try:
            add(srv.fetchItems(path))
        except Exception as e:                   # one way failing mustn't hide what the other finds
            log.debug("Plex %s: %s", path, e)
    if not seen:
        try:
            keys = {str(t.parentRatingKey) for t in srv.fetchItems(f"/library/metadata/{rid}/allLeaves")
                    if getattr(t, "parentRatingKey", None)}
            for k in sorted(keys):
                add(srv.fetchItems(f"/library/metadata/{k}"))
        except Exception as e:
            log.debug("Plex tracks of %s: %s", rid, e)
    first = list(seen.values())
    return sorted(first, key=lambda a: (a.year or 9999, (a.title or "").lower()))


class PlexSource(EngineSource):
    id = "plex"
    name = "Plex"

    def __init__(self, ctx):
        super().__init__(ctx)
        saved = ctx.store.get(SETTINGS_KEY) or {}
        self.url = ((saved.get("url") if saved else self.cfg.get("url")) or "").rstrip("/")
        self.token = (saved.get("token") if saved else self.cfg.get("token")) or ""
        self.from_web = bool(saved)
        self._server: Any = None
        self.last_error = ""
        self._pin: Any = None              # a sign-in with Plex in progress
        self._pin_started = 0.0
        self._connect_lock = asyncio.Lock()
        self.queue: list[dict[str, Any]] = []
        self.queue_index: int | None = None
        self._last_position = 0.0
        self.server_name = ""

    # ------------------------------------------------------------ connection
    async def start(self) -> None:
        ok, reason = self.engine_ready()
        if not ok:
            self.set_available(False, reason)
            return
        if not (self.url and self.token):
            self.set_available(False, "Not connected to a Plex server yet: Plex → Connect")
            return
        self.set_available(True)
        asyncio.create_task(self._probe())

    async def _probe(self) -> None:
        try:
            await self.server()
            self.last_error = ""
        except SourceError as exc:
            self.last_error = str(exc)
            log.warning("Plex not reachable yet: %s", exc)

    async def server(self) -> Any:
        if self._server is not None:
            return self._server
        async with self._connect_lock:
            if self._server is None:
                from plexapi.server import PlexServer

                try:
                    self._server = await asyncio.to_thread(PlexServer, self.url, self.token, timeout=15)
                except Exception as exc:
                    raise SourceError(f"Cannot reach Plex at {self.url}: {exc}", 502) from exc
                self.server_name = self._server.friendlyName
                log.info("Connected to Plex server '%s'", self.server_name)
                self.ctx.manager.notify(True)
        return self._server

    async def _run(self, fn, *args, **kwargs) -> Any:
        srv = await self.server()
        try:
            return await asyncio.to_thread(fn, srv, *args, **kwargs)
        except SourceError:
            raise
        except Exception as exc:
            if exc.__class__.__name__ == "NotFound":
                raise SourceError("Not found in Plex", 404) from exc
            raise SourceError(f"Plex request failed: {exc}", 502) from exc

    # ------------------------------------------------------------ connecting from the web page
    def connection(self) -> dict[str, Any]:
        """What Plex → Connect shows. The token itself never leaves the Pi."""
        return {
            "url": self.url,
            "token_set": bool(self.token),
            "connected": self._server is not None,
            "server": self.server_name,
            "error": self.last_error,
            "from": "web" if self.from_web else ("config" if self.url else ""),
            "signing_in": self._pin is not None,
        }

    @staticmethod
    def _connect_sync(url: str, token: str) -> Any:
        from plexapi.server import PlexServer

        return PlexServer(url, token, timeout=10)

    async def configure(self, url: str, token: str | None) -> dict[str, Any]:
        """Connect to this server (token None = keep the current one); saved only when the connection works.
        An empty address disconnects (the config file's settings, if any, are used again after a restart)."""
        url = (url or "").strip().rstrip("/")
        if url and not url.startswith(("http://", "https://")):
            url = "http://" + url
        if url and ":" not in url.split("://", 1)[1]:
            url += ":32400"
        token = self.token if token is None else token.strip()
        if not url:
            await self._drop()
            self.ctx.store.set(SETTINGS_KEY, None)
            self.url, self.token, self.from_web, self.server_name, self.last_error = "", "", False, "", ""
            self.set_available(False, "Not connected to a Plex server yet: Plex → Connect")
            return self.connection()
        if not token:
            raise SourceError("Give the Plex token (or sign in with Plex)", 400)
        try:
            server = await asyncio.to_thread(self._connect_sync, url, token)
        except Exception as exc:
            msg = str(exc)
            if "401" in msg or "Unauthorized" in msg:
                raise SourceError("Plex doesn't accept this token", 400) from exc
            raise SourceError(f"Can't reach a Plex server at {url}: {msg[:200]}", 400) from exc
        await self._drop()
        self.url, self.token, self._server, self.from_web = url, token, server, True
        self.server_name, self.last_error = server.friendlyName, ""
        self.ctx.store.set(SETTINGS_KEY, {"url": url, "token": token})
        log.info("Connected to Plex server '%s' at %s", self.server_name, url)
        self.set_available(True)
        self.ctx.manager.notify(True)
        return self.connection()

    async def _drop(self) -> None:
        """Forget the current server (stop playing from it first)."""
        if self.active:
            await self.ctx.manager.deactivate_all()
        self._server = None
        self.queue, self.queue_index = [], None

    # Sign in with Plex: the player asks plex.tv for a sign-in page, you approve it there, and the player gets a
    # token of its own (it shows up in Plex → Settings → Authorized Devices as "JPlay <name>").
    def _client_id(self) -> str:
        cid = self.ctx.store.get(CLIENT_ID_KEY)
        if not cid:
            import secrets

            cid = "jplay-" + secrets.token_hex(8)
            self.ctx.store.set(CLIENT_ID_KEY, cid)
        return cid

    def _plex_headers(self) -> dict[str, str]:
        name = self.ctx.config["server"]["name"]
        return {"X-Plex-Client-Identifier": self._client_id(), "X-Plex-Product": "JPlay",
                "X-Plex-Device-Name": f"JPlay {name}", "X-Plex-Device": "Raspberry Pi", "X-Plex-Platform": "Linux"}

    async def sign_in_start(self) -> dict[str, Any]:
        import time

        from plexapi.myplex import MyPlexPinLogin

        def start():
            pin = MyPlexPinLogin(oauth=True, headers=self._plex_headers())
            return pin, pin.oauthUrl()
        try:
            self._pin, url = await asyncio.to_thread(start)
        except Exception as exc:
            raise SourceError(f"Can't reach plex.tv: {exc}", 502) from exc
        self._pin_started = time.time()
        return {"url": url}

    async def sign_in_check(self) -> dict[str, Any]:
        """Has the sign-in been approved? Then: {"done": True, "servers": [...]} to pick from."""
        import time

        if self._pin is None:
            raise SourceError("No sign-in in progress", 409)
        if time.time() - self._pin_started > PIN_TIMEOUT:
            self._pin = None
            raise SourceError("The sign-in took too long; start again", 409)
        try:
            ok = await asyncio.to_thread(self._pin.checkLogin)
        except Exception as exc:
            raise SourceError(f"Can't reach plex.tv: {exc}", 502) from exc
        if not ok:
            return {"done": False}
        token = self._pin.token
        self._pin = None
        servers = await asyncio.to_thread(self._servers_for, token)
        self._signed_in_token = token
        # (the servers' tokens stay on the Pi)
        return {"done": True, "servers": [{k: v for k, v in x.items() if k != "token"} for x in servers]}

    def _servers_for(self, token: str) -> list[dict[str, Any]]:
        """The Plex servers this account can use, with the addresses to try (local ones first)."""
        from plexapi.myplex import MyPlexAccount

        account = MyPlexAccount(token=token)
        out = []
        for res in account.resources():
            if "server" not in (res.provides or ""):
                continue
            conns = sorted(res.connections or [], key=lambda c: (not c.local, c.relay))
            urls = [f"http://{c.address}:{c.port}" if c.local and c.address else c.uri for c in conns if not c.relay]
            out.append({"name": res.name, "id": res.clientIdentifier, "owned": bool(res.owned),
                        "urls": list(dict.fromkeys(urls)), "token": res.accessToken or token})
        return out

    async def sign_in_choose(self, server_id: str) -> dict[str, Any]:
        """Connect to one of the servers found after signing in: the first of its addresses that answers."""
        token = getattr(self, "_signed_in_token", None)
        if not token:
            raise SourceError("Sign in with Plex first", 409)
        servers = await asyncio.to_thread(self._servers_for, token)
        server = next((s for s in servers if s["id"] == server_id), None)
        if not server:
            raise SourceError("That server isn't on this Plex account", 404)
        last = None
        for url in server["urls"]:
            try:
                return await self.configure(url, server["token"])
            except SourceError as exc:
                last = exc
        raise last or SourceError("None of the server's addresses answer", 502)

    def details(self) -> dict[str, Any]:
        return {
            "server": self.server_name,
            "queue_length": len(self.queue),
            "queue_index": self.queue_index,
        }

    def capabilities(self) -> Capabilities:
        has = bool(self.queue)
        return Capabilities(
            play=has, pause=has, stop=has, seek=has, next=len(self.queue) > 1, previous=has
        )

    # ------------------------------------------------------------ browsing
    async def libraries(self) -> list[dict[str, Any]]:
        def fn(srv):
            return [
                {"key": str(s.key), "title": s.title}
                for s in srv.library.sections()
                if s.type == "artist"
            ]

        return await self._run(fn)

    async def browse_section(self, section_key: str, kind: str, start: int, size: int, sort: str) -> dict[str, Any]:
        if kind not in ("artist", "album"):
            raise SourceError("kind must be 'artist' or 'album'")
        plex_type = PLEX_TYPE[kind]
        sort = {"title": "titleSort", "added": "addedAt:desc", "year": "year:desc", "played": "lastViewedAt:desc"}.get(
            sort, "titleSort"
        )
        section = int(section_key)

        def fn(srv):
            # Raw library endpoints: one request per page, no client-side filter validation.
            key = f"/library/sections/{section}/all?type={plex_type}&sort={sort}"
            items = srv.fetchItems(key, container_start=start, container_size=size, maxresults=size)
            head = srv.query(
                f"/library/sections/{section}/all?type={plex_type}"
                "&X-Plex-Container-Start=0&X-Plex-Container-Size=0"
            )
            return {
                "total": int(head.attrib.get("totalSize") or head.attrib.get("size") or len(items)),
                "start": start,
                "items": [_item(i) for i in items],
            }

        return await self._run(fn)

    async def children(self, key: str) -> dict[str, Any]:
        """Albums of an artist, tracks of an album or a playlist."""

        def fn(srv):
            obj = srv.fetchItem(int(key))
            kind = obj.type
            if kind == "artist":
                kids = _artist_albums(srv, obj)
            elif kind == "album":
                kids = obj.tracks()
            elif kind == "playlist":
                kids = [i for i in obj.items() if getattr(i, "type", "") == "track"]
            else:
                raise SourceError(f"Cannot open a {kind}")
            return {"item": _item(obj), "items": [_item(k) for k in kids]}

        return await self._run(fn)

    async def playlists(self) -> list[dict[str, Any]]:
        return await self._run(lambda srv: [_item(p) for p in srv.playlists(playlistType="audio")])

    async def recent(self, section_key: str | None, size: int = 30) -> list[dict[str, Any]]:
        def fn(srv):
            if section_key:
                sections = [int(section_key)]
            else:
                sections = [s.key for s in srv.library.sections() if s.type == "artist"]
            albums = []
            for sec in sections:
                albums += srv.fetchItems(
                    f"/library/sections/{sec}/all?type=9&sort=addedAt:desc",
                    container_start=0, container_size=size, maxresults=size,
                )
            albums.sort(key=lambda a: a.addedAt.timestamp() if a.addedAt else 0, reverse=True)
            return [_item(a) for a in albums[:size]]

        return await self._run(fn)

    async def search(self, query: str) -> dict[str, list[dict[str, Any]]]:
        if not query.strip():
            return {t: [] for t in MUSIC_TYPES}

        def fn(srv):
            out: dict[str, list[dict[str, Any]]] = {t: [] for t in MUSIC_TYPES}
            for mediatype in MUSIC_TYPES:
                for obj in srv.search(query, mediatype=mediatype, limit=20):
                    if getattr(obj, "type", "") == mediatype:
                        out[mediatype].append(_item(obj))
            return out

        return await self._run(fn)

    async def fetch_art(self, path: str, size: int) -> tuple[bytes, str]:
        if not path.startswith("/library/") and not path.startswith("/playlists/") and not path.startswith(
            "/photo/"
        ):
            raise SourceError("Invalid artwork path", 400)
        srv = await self.server()
        size = max(64, min(int(size), 1200))
        url = srv.transcodeImage(srv.url(path, includeToken=True), size, size)
        resp = await self.ctx.http.get(url, timeout=20)
        if resp.status_code != 200:
            raise SourceError("Artwork not available", 404)
        return resp.content, resp.headers.get("content-type", "image/jpeg")

    # ------------------------------------------------------------ playback
    async def play_item(self, key: str, index: int = 0, shuffle: bool = False) -> None:
        """Play an album, artist, playlist or single track, starting at `index`."""

        def fn(srv):
            obj = srv.fetchItem(int(key))
            kind = obj.type
            if kind in ("album", "artist"):
                tracks = obj.tracks()
            elif kind == "playlist":
                tracks = [i for i in obj.items() if getattr(i, "type", "") == "track"]
            elif kind == "track":
                tracks = [obj]
            else:
                raise SourceError(f"Cannot play a {kind}")
            queue = []
            for t in tracks:
                if not t.media or not t.media[0].parts:
                    continue
                part = t.media[0].parts[0]
                media = t.media[0]
                queue.append(
                    {
                        "key": str(t.ratingKey),
                        "title": t.title or "",
                        "artist": t.originalTitle or t.grandparentTitle or "",
                        "album": t.parentTitle or "",
                        "duration": (t.duration or 0) / 1000 or None,
                        "thumb": t.parentThumb or t.thumb or "",
                        "url": srv.url(part.key, includeToken=True),
                        "codec": media.audioCodec or "",
                        "bitrate": media.bitrate or 0,
                        "track_number": t.index,
                    }
                )
            return queue

        queue = await self._run(fn)
        if not queue:
            raise SourceError("Nothing playable there")
        index = max(0, min(int(index), len(queue) - 1))
        if shuffle:
            first = queue.pop(index)
            random.shuffle(queue)
            queue.insert(0, first)
            index = 0
        await self.play_queue(queue, index)

    async def play_queue(self, queue: list[dict[str, Any]], index: int) -> None:
        await self.claim()
        # A new queue: forget the old position so nothing gets scrobbled twice.
        self.queue = queue
        self.queue_index = None
        self._last_position = 0.0
        self._show(index, PlayState.LOADING)
        playlist = self.ctx.runtime_dir / "plex-queue.m3u"
        _write_private(playlist, "#EXTM3U\n" + "".join(f"{t['url']}\n" for t in queue))
        await self.engine.command("set_property", "pause", False)
        await self.engine.command("loadlist", str(playlist), "replace")
        if index:
            await self.engine.command("playlist-play-index", index)

    async def play_index(self, index: int) -> None:
        if not self.queue:
            raise SourceError("The Plex queue is empty")
        if not self.owns_engine:
            await self.play_queue(self.queue, index)
            return
        await self.engine.command("playlist-play-index", int(index))

    def _show(self, index: int, state: PlayState | None = None) -> None:
        if not (0 <= index < len(self.queue)):
            return
        t = self.queue[index]
        self.queue_index = index
        self.reset_now_playing(
            state=state or self.np.state,
            title=t["title"],
            artist=t["artist"],
            album=t["album"],
            art_url=art_url(t["thumb"], 600),
            duration=t["duration"],
            position=0.0,
            track_number=t["track_number"],
            queue_index=index,
            queue_length=len(self.queue),
            extra={"key": t["key"], "codec": t["codec"], "bitrate": t["bitrate"], "server": self.server_name},
        )

    async def play(self) -> None:
        if self.owns_engine and self.np.state == PlayState.PAUSED:
            await self.resume()
        elif self.queue:
            await self.play_index(self.queue_index or 0)
        else:
            raise SourceError("Pick something to play in Plex first")

    async def next(self) -> None:
        await self.cmd("playlist-next", "weak")

    async def previous(self) -> None:
        if (self.np.position or 0) > 3 or not self.queue_index:
            await self.seek(0)
        else:
            await self.cmd("playlist-prev", "weak")

    def queue_view(self) -> dict[str, Any]:
        return {
            "index": self.queue_index,
            "items": [
                {k: t[k] for k in ("key", "title", "artist", "album", "duration")} | {"thumb": art_url(t["thumb"])}
                for t in self.queue
            ],
        }

    # ------------------------------------------------------------ mpv callbacks
    async def on_mpv_property(self, name: str, value: Any) -> None:
        if name == "time-pos" and value is not None:
            self._last_position = float(value)
        await super().on_mpv_property(name, value)

    async def on_playlist_pos(self, index: int) -> None:
        previous = self.queue_index
        if previous is not None and previous != index:
            self._maybe_scrobble(previous, self._last_position)
        self._last_position = 0.0
        if index != previous:
            self._show(index)

    async def on_idle(self) -> None:
        if self.queue_index is not None:
            self._maybe_scrobble(self.queue_index, self._last_position)

    def _maybe_scrobble(self, index: int, played: float) -> None:
        if not self.cfg.get("scrobble", True) or not (0 <= index < len(self.queue)):
            return
        t = self.queue[index]
        duration = t["duration"] or 0
        if duration and played >= min(duration * 0.5, 240):
            asyncio.create_task(self._scrobble(t["key"]))

    async def _scrobble(self, key: str) -> None:
        try:
            await self._run(
                lambda srv: srv.query(f"/:/scrobble?identifier=com.plexapp.plugins.library&key={key}")
            )
        except SourceError as exc:
            log.debug("Scrobble failed: %s", exc)


def _write_private(path: Path, text: str) -> None:
    """The playlist holds URLs with the Plex token, so keep it readable by us only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
