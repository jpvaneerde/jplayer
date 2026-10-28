"""The hub's web app (port 8090): the API the displays use, and the Spins, Lyrics and Settings pages.

No passwords: like the players and displays, it is meant for the home network only.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..common import system
from ..common.announce import HUB_TYPE, Announcer
from ..common.updates import UpdateError
from . import db, importer, lyrics, records, records_sync, spins

log = logging.getLogger(__name__)
WEB_DIR = system.WEB_DIR / "hub"
PORT = 8090
STATS_CACHE_SECONDS = 600        # totals for a year or more are worked out at most every 10 minutes
PROCESS_EVERY = 20               # seconds between runs of the Spins worker
SYNC_EVERY = 60                  # send new and changed plays to JPlay Records (when that's on)
REMATCH_EVERY = 6 * 3600         # match "heard" plays to your records again (JPlay Records connected)


# ---------------------------------------------------------------- request bodies
class EventsBody(BaseModel):
    unit: dict[str, Any]
    events: list[Any] = []


class RoomBody(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    priority: int = Field(1, ge=0, le=100)
    formats: list[str] = []
    old_name: str | None = None


class PlayFix(BaseModel):
    heard: bool = False             # "not one of my records"
    physical: str | None = None     # "it was a record / a CD": vinyl | cd | cassette
    record: int | None = None       # "it was this copy" (one of the suggested copies)
    auto: bool = False              # undo: let the matching decide again


class LyricsFix(BaseModel):
    offset: float | None = None
    text: str | None = None


class RecordsBody(BaseModel):
    url: str = ""
    token: str | None = None        # empty/None = keep the saved token


class SyncBody(BaseModel):
    on: bool


class ImportRecordsBody(BaseModel):
    url: str
    token: str


class ImportDisplayBody(BaseModel):
    address: str
    room: str


class ImportFavouritesBody(BaseModel):
    source: str
    target: str


class UpdateBody(BaseModel):
    target: str | None = None


class UpdateSettingsBody(BaseModel):
    channel: str | None = Field(None, pattern="^(releases|main)$")
    auto: str | None = Field(None, pattern="^(off|night)$")


def _name() -> str:
    return str(db.setting("name", "") or "") or f"JPlay hub ({socket.gethostname()})"


async def _worker() -> None:
    """Turn events into plays every few seconds; now and then match plays to your records again."""
    await asyncio.to_thread(spins.merge_duplicates)
    last_rematch = last_sync = 0.0
    loop = asyncio.get_running_loop()
    while True:
        try:
            await asyncio.to_thread(spins.process_events)
            if records_sync.enabled() and loop.time() - last_sync > SYNC_EVERY:
                last_sync = loop.time()
                await asyncio.to_thread(records_sync.sync_once)
            if records.enabled() and loop.time() - last_rematch > REMATCH_EVERY:
                last_rematch = loop.time()
                n = await asyncio.to_thread(spins.rematch)
                if n:
                    log.info("Spins: %s plays matched again", n)
        except Exception:
            log.exception("Spins worker")
        await asyncio.sleep(PROCESS_EVERY)


async def _auto_update(app: FastAPI) -> None:
    """On a Pi that is only a hub: install a waiting update at night when that's switched on."""
    while True:
        await asyncio.sleep(60)
        if system.primary_role() != "hub":
            continue
        try:
            target = await asyncio.to_thread(app.state.updates.auto_target, False)
            if target:
                log.info("Installing %s automatically", target)
                await asyncio.to_thread(app.state.updates.install, target)
        except Exception as exc:
            log.warning("Automatic update: %s", exc)


def create_app(data_dir: Path | None = None, port: int = PORT, announce: bool = True) -> FastAPI:
    data_dir = data_dir or system.state_dir("hub")
    covers_dir = data_dir / "covers"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init(str(data_dir / "hub.db"))
        lyrics.init()
        tasks = [asyncio.create_task(_worker(), name="spins"), asyncio.create_task(_auto_update(app), name="update")]
        announcer = Announcer(_name(), port, {"id": records_sync.hub_key(), "version": app_version(), "path": "/api"},
                              HUB_TYPE) if announce else None
        if announcer:
            await announcer.start()
        log.info("JPlay hub ready on port %s (data in %s)", port, data_dir)
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            if announcer:
                await announcer.shutdown()
            db.close()

    app = FastAPI(title="JPlay hub", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)
    app.state.updates = system.updates()

    def app_version() -> str:
        v = app.state.updates.version()["version"]
        return __version__ if v in ("unknown", "") else v

    stats_cache: dict[int, tuple[float, dict]] = {}

    def t(fn, *a, **kw):
        return asyncio.to_thread(fn, *a, **kw)

    @app.exception_handler(ValueError)
    async def bad_value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    # ------------------------------------------------------------ for the displays
    @app.get("/api/info")
    async def info() -> dict:
        return {"id": await t(records_sync.hub_key), "name": _name(), "version": app_version(), "kind": "hub",
                "records": records.enabled(),
                "roles": system.roles()}

    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True}

    @app.post("/api/events")
    async def post_events(request: Request, body: EventsBody) -> dict:
        """A display reports what its room hears and plays: {"unit": {"id", "name", "room", ...}, "events": [...]}."""
        address = request.client.host if request.client else ""
        n = await t(spins.ingest, body.unit, body.events, address)
        return {"accepted": n, "room": str(body.unit.get("room") or "")}

    @app.get("/api/lyrics")
    async def get_lyrics(artist: str = "", title: str = "", album: str = "", duration: float | None = None):
        ans = await t(lyrics.lookup, artist, title, album, duration if duration and duration > 0 else None)
        if ans.get("unavailable"):
            # LRCLIB couldn't be reached from here: say so, so the display asks LRCLIB itself
            return JSONResponse({"error": "LRCLIB could not be reached from the hub"}, status_code=502)
        return ans

    # ------------------------------------------------------------ Spins
    @app.get("/api/now")
    async def api_now() -> dict:
        return {"rooms": await t(spins.now_playing)}

    @app.get("/api/plays")
    async def api_plays(kind: str = "", room: str = "", q: str = "", limit: int = 60, offset: int = 0) -> dict:
        return await t(spins.list_plays, kind, room, q.strip()[:100], max(1, min(limit, 200)), max(0, offset))

    @app.get("/api/spins")
    async def api_spins(limit: int = 30, room: str = "") -> dict:
        return {"spins": await t(spins.recent_spins, max(1, min(limit, 100)), room)}

    @app.get("/api/stats")
    async def api_stats(days: int = 30, since: int | None = None) -> dict:
        days = max(0, days)
        if since is not None or 0 < days <= 31:
            return await t(spins.stats, days, since)
        # A year or everything: seconds of work after years of listening, so kept for a few minutes
        hit = stats_cache.get(days)
        if hit and time.monotonic() - hit[0] < STATS_CACHE_SECONDS:
            return hit[1]
        result = await t(spins.stats, days)
        stats_cache[days] = (time.monotonic(), result)
        return result

    @app.put("/api/plays/{play_id}")
    async def fix_play(play_id: int, body: PlayFix) -> dict:
        if body.auto:
            await t(spins.reset_play, play_id)
        elif body.heard:
            await t(spins.mark_heard, play_id)
        elif body.physical:
            if not await t(spins.mark_physical, play_id, body.physical):
                raise HTTPException(400, "vinyl, cd or cassette")
        elif body.record is not None:
            if not await t(spins.set_play_record, play_id, body.record):
                raise HTTPException(404, "That copy wasn't one of the suggestions")
        await t(records_sync.changed, play_id)
        return {"ok": True}

    @app.delete("/api/plays/{play_id}")
    async def delete_play(play_id: int) -> dict:
        await t(spins.delete_play, play_id)
        return {"ok": True}

    @app.post("/api/rematch")
    async def api_rematch(days: int = 365) -> dict:
        merged = await t(spins.merge_duplicates, days)
        return {"changed": merged + await t(spins.rematch, days), "records": records.enabled()}

    @app.get("/api/cover")
    async def cover(u: str):
        """A cover of one of your records (fetched from JPlay Records with its token, and kept)."""
        path = await t(records.cover_path, covers_dir, u)
        if not path:
            raise HTTPException(404, "No cover")
        return FileResponse(path, headers={"Cache-Control": "public, max-age=2592000"})

    # ------------------------------------------------------------ rooms and units
    @app.get("/api/rooms")
    async def get_rooms() -> dict:
        return {"rooms": await t(spins.rooms), "units": await t(spins.units)}

    @app.put("/api/rooms")
    async def put_room(body: RoomBody) -> dict:
        await t(spins.save_room, body.name, body.priority, body.formats, body.old_name)
        return await get_rooms()

    @app.delete("/api/rooms/{name}")
    async def delete_room(name: str) -> dict:
        await t(spins.delete_room, name)
        return await get_rooms()

    @app.delete("/api/units/{unit_id}")
    async def forget_unit(unit_id: str) -> dict:
        await t(spins.forget_unit, unit_id)
        return await get_rooms()

    # ------------------------------------------------------------ lyrics
    @app.get("/api/lyrics/songs")
    async def lyrics_songs(q: str = "", only: str = "", limit: int = 100, offset: int = 0) -> dict:
        return {**await t(lyrics.songs, q.strip()[:100], only, max(1, min(limit, 300)), max(0, offset)),
                "stats": await t(lyrics.stats)}

    @app.get("/api/lyrics/song")
    async def lyrics_song(key: str) -> dict:
        s = await t(lyrics.song, key)
        if not s:
            raise HTTPException(404, "Unknown song")
        return s

    @app.put("/api/lyrics/song")
    async def lyrics_fix(key: str, body: LyricsFix) -> dict:
        ok = True
        if body.offset is not None:
            ok = await t(lyrics.set_offset, key, body.offset)
        if body.text is not None and ok:
            ok = await t(lyrics.set_text, key, body.text)
        if not ok:
            raise HTTPException(404, "Unknown song")
        return await t(lyrics.song, key)

    @app.delete("/api/lyrics/song")
    async def lyrics_forget(key: str) -> dict:
        await t(lyrics.forget, key)
        return {"ok": True}

    # ------------------------------------------------------------ settings
    @app.get("/api/settings")
    async def get_settings() -> dict:
        cfg = records.config()
        return {"name": _name(), "records": {"url": str(db.setting("records_url", "") or ""), "token_set": bool(cfg),
                                             "enabled": bool(cfg), "sync": bool(db.setting("records_sync", True)),
                                             "sync_status": records_sync.status}}

    @app.put("/api/settings/records")
    async def put_records(body: RecordsBody) -> dict:
        db.set_setting("records_url", body.url.strip().rstrip("/"))
        if body.token is not None and (body.token.strip() or not body.url.strip()):
            db.set_setting("records_token", body.token.strip())
        result = await t(records.test) if records.enabled() else {"ok": True}
        return {**await get_settings(), "test": result}

    @app.put("/api/settings/records/sync")
    async def put_records_sync(body: SyncBody) -> dict:
        db.set_setting("records_sync", body.on)
        if body.on and records.enabled():
            await t(records_sync.sync_once)
        return await get_settings()

    @app.post("/api/settings/records/test")
    async def test_records() -> dict:
        return await t(records.test)

    # ------------------------------------------------------------ import
    @app.get("/api/import")
    async def import_status() -> dict:
        return importer.status

    def start(what: str, fn, *args) -> dict:
        if not importer.run_in_background(what, fn, *args):
            raise HTTPException(409, "An import is running; wait for it to finish")
        return importer.status

    @app.post("/api/import/records")
    async def import_records(body: ImportRecordsBody) -> dict:
        return start("records", importer.from_records, body.url, body.token)

    @app.post("/api/import/display")
    async def import_display(body: ImportDisplayBody) -> dict:
        return start("display", importer.from_display, body.address, body.room)

    @app.post("/api/import/favourites")
    async def import_favourites(body: ImportFavouritesBody) -> dict:
        return start("favourites", importer.favourites, body.source, body.target)

    # ------------------------------------------------------------ version and updates (one updater per Pi)
    @app.get("/api/version")
    async def api_version() -> dict:
        return await t(app.state.updates.version)

    @app.get("/api/update")
    async def api_update() -> dict:
        return await t(app.state.updates.status)

    @app.post("/api/update/check")
    async def api_update_check():
        try:
            await t(app.state.updates.check)
        except UpdateError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return {"ok": True}

    @app.post("/api/update/install")
    async def api_update_install(body: UpdateBody | None = None):
        st = await t(app.state.updates.status)
        target = (body.target if body else None) or st["target"]
        if not target:
            return JSONResponse({"error": "Already up to date"}, status_code=409)
        try:
            await t(app.state.updates.install, target)
        except UpdateError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return {"ok": True, "target": target}

    @app.put("/api/update/settings")
    async def api_update_settings(body: UpdateSettingsBody) -> dict:
        await t(app.state.updates.save_settings, body.channel, body.auto)
        return await t(app.state.updates.status)

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def page_spins():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/lyrics", response_class=HTMLResponse)
    async def page_lyrics():
        return FileResponse(WEB_DIR / "lyrics.html")

    @app.get("/settings", response_class=HTMLResponse)
    async def page_settings():
        return FileResponse(WEB_DIR / "settings.html")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return RedirectResponse("/static/icon.svg", status_code=301)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app
