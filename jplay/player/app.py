"""FastAPI application: REST API, live WebSocket events and the web UI."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from .config import Config
from ..common.announce import Announcer
from .core.context import AppContext
from .core.events import EventHub
from .core.manager import SourceManager
from .core.mpv import MpvEngine
from .core.output import Output
from .core.store import StateStore
from ..common import system
from ..common.updates import UpdateError
from .core.volume import Volume
from .sources.base import SourceError
from .sources.bluetooth import BluetoothSource
from .sources.cd import CdSource, parse_range
from .sources.plex import PlexSource
from .sources.radio import RadioSource
from .sources.spotify import SpotifySource

log = logging.getLogger(__name__)
WEB_DIR = system.WEB_DIR / "player"


def _usable_dir(preferred: str, fallback: Path) -> Path:
    """Use the configured directory, or a per-user fallback when it is not writable (dev PCs)."""
    for candidate in (Path(preferred), fallback):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("x")
            probe.unlink()
            return candidate
        except OSError:
            continue
    raise RuntimeError(f"No writable directory for {preferred}")


def build_context(config: Config) -> AppContext:
    paths = config["paths"]
    state_dir = _usable_dir(paths["state_dir"], Path.home() / ".local" / "state" / "jplay")
    runtime_root = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    runtime_dir = _usable_dir(paths["runtime_dir"], Path(runtime_root) / f"jplay-{os.getuid()}")
    audio = config["audio"]
    return AppContext(
        config=config,
        store=StateStore(state_dir / "state.json"),
        hub=EventHub(),
        # The output device and mixer are filled in by Output once the sound cards are known.
        engine=MpvEngine(audio["mpv_binary"], "null", audio.get("mpv_extra_args", []), runtime_dir),
        volume=Volume("", ""),
        http=httpx.AsyncClient(headers={"User-Agent": "JPlay/0.1"}, follow_redirects=True),
        state_dir=state_dir,
        runtime_dir=runtime_dir,
        hook_token=secrets.token_urlsafe(24),
    )


# ---------------------------------------------------------------- request bodies
class SeekBody(BaseModel):
    position: float = Field(ge=0)


class VolumeBody(BaseModel):
    volume: int = Field(ge=0, le=100)


class StationBody(BaseModel):
    uuid: str | None = None
    station: dict[str, Any] | None = None


class OrderBody(BaseModel):
    uuids: list[str]


class PlexPlayBody(BaseModel):
    key: str
    index: int = 0
    shuffle: bool = False


class PlexConnectionBody(BaseModel):
    url: str = ""
    token: str | None = None        # None: keep the saved token


class PlexServerBody(BaseModel):
    id: str


class CdPlayBody(BaseModel):
    track: int = 1


class OutputBody(BaseModel):
    device: str = Field(min_length=1, description='"auto", "null" or an ALSA device like "hw:CARD=DAC,DEV=0"')
    mixer_control: str | None = Field(None, description='Hardware volume control; "" for fixed volume')


class DiscoverableBody(BaseModel):
    on: bool = True


class UpdateInstallBody(BaseModel):
    target: str | None = None


class UpdateSettingsBody(BaseModel):
    channel: str | None = Field(None, pattern="^(releases|main)$")
    auto: str | None = Field(None, pattern="^(off|night)$")


# ---------------------------------------------------------------- routes
def api_router() -> APIRouter:
    r = APIRouter(prefix="/api/v1")

    def ctx(request: Request) -> AppContext:
        return request.app.state.ctx

    def src(request: Request, source_id: str):
        return ctx(request).manager.get(source_id)

    # -- general
    @r.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True}

    @r.get("/status")
    async def status(request: Request) -> dict[str, Any]:
        return ctx(request).manager.snapshot()

    @r.get("/info")
    async def info(request: Request) -> dict[str, Any]:
        """Who this player is: a stable id (for Home Assistant), its name and version."""
        c = ctx(request)
        return {
            "id": player_id(c),
            "name": c.config["server"]["name"],
            "version": app_version(request.app),
            "sources": [{"id": s.id, "name": s.name} for s in c.manager.sources.values()],
        }

    @r.post("/standby")
    async def standby(request: Request) -> dict[str, Any]:
        """Stop whatever plays and go idle (no source)."""
        await ctx(request).manager.deactivate_all()
        return ctx(request).manager.snapshot()

    @r.post("/sources/{source_id}/activate")
    async def activate(request: Request, source_id: str) -> dict[str, Any]:
        await ctx(request).manager.activate(source_id)
        return ctx(request).manager.snapshot()

    @r.post("/player/seek")
    async def seek(request: Request, body: SeekBody) -> dict[str, Any]:
        await ctx(request).manager.transport("seek", body.position)
        return {"ok": True}

    @r.post("/player/{command}")
    async def transport(request: Request, command: str) -> dict[str, Any]:
        await ctx(request).manager.transport(command)
        return {"ok": True}

    @r.get("/volume")
    async def get_volume(request: Request) -> dict[str, Any]:
        c = ctx(request)
        c.manager.volume = await c.volume.get()
        return {"volume": c.manager.volume, "available": c.volume.available}

    @r.put("/volume")
    async def set_volume(request: Request, body: VolumeBody) -> dict[str, Any]:
        c = ctx(request)
        if not c.volume.available:
            raise SourceError("Volume is fixed (no audio.mixer_control configured)", 409)
        return {"volume": await c.manager.set_volume(body.volume), "available": True}

    # -- output (which DAC)
    @r.get("/output")
    async def get_output(request: Request) -> dict[str, Any]:
        return await ctx(request).output.choices()

    @r.put("/output")
    async def set_output(request: Request, body: OutputBody) -> dict[str, Any]:
        c = ctx(request)
        await c.output.select(body.device, body.mixer_control)
        return await c.output.choices()

    # -- radio
    @r.get("/radio/favourites")
    async def radio_favourites(request: Request) -> list[dict[str, Any]]:
        return src(request, "radio").favourites()

    @r.post("/radio/favourites")
    async def radio_add(request: Request, station: dict[str, Any]) -> list[dict[str, Any]]:
        return src(request, "radio").add_favourite(station)

    @r.delete("/radio/favourites/{uuid}")
    async def radio_remove(request: Request, uuid: str) -> list[dict[str, Any]]:
        return src(request, "radio").remove_favourite(uuid)

    @r.put("/radio/favourites/order")
    async def radio_order(request: Request, body: OrderBody) -> list[dict[str, Any]]:
        return src(request, "radio").reorder_favourites(body.uuids)

    @r.get("/radio/search")
    async def radio_search(
        request: Request, name: str = "", country: str = "", tag: str = "", limit: int = 40
    ) -> list[dict[str, Any]]:
        return await src(request, "radio").search(name=name, countrycode=country, tag=tag, limit=limit)

    @r.post("/radio/play")
    async def radio_play(request: Request, body: StationBody) -> dict[str, Any]:
        radio = src(request, "radio")
        station = body.station
        if station is None and body.uuid:
            station = next((f for f in radio.favourites() if f["uuid"] == body.uuid), None)
        if station is None:
            raise SourceError("Give a favourite 'uuid' or a 'station' object")
        await radio.play_station(station)
        return {"ok": True}

    # -- plex: the connection (address and token, or signing in with Plex)
    @r.get("/plex/connection")
    async def plex_connection(request: Request) -> dict[str, Any]:
        return src(request, "plex").connection()

    @r.put("/plex/connection")
    async def plex_connect(request: Request, body: PlexConnectionBody) -> dict[str, Any]:
        """{"url": "http://192.168.1.10:32400", "token": "..."}: test and save; {"url": ""} disconnects."""
        return await src(request, "plex").configure(body.url, body.token)

    @r.post("/plex/sign-in")
    async def plex_sign_in(request: Request) -> dict[str, Any]:
        """Start signing in with Plex: open the returned url and approve; then poll GET /plex/sign-in."""
        return await src(request, "plex").sign_in_start()

    @r.get("/plex/sign-in")
    async def plex_sign_in_check(request: Request) -> dict[str, Any]:
        return await src(request, "plex").sign_in_check()

    @r.post("/plex/sign-in/server")
    async def plex_sign_in_server(request: Request, body: PlexServerBody) -> dict[str, Any]:
        return await src(request, "plex").sign_in_choose(body.id)

    @r.get("/plex/libraries")
    async def plex_libraries(request: Request) -> list[dict[str, Any]]:
        return await src(request, "plex").libraries()

    @r.get("/plex/sections/{section_key}/{kind}")
    async def plex_section(
        request: Request, section_key: str, kind: str, start: int = 0, size: int = 60, sort: str = "title"
    ) -> dict[str, Any]:
        return await src(request, "plex").browse_section(section_key, kind, max(0, start), max(1, min(size, 200)), sort)

    @r.get("/plex/items/{key}/children")
    async def plex_children(request: Request, key: str) -> dict[str, Any]:
        return await src(request, "plex").children(key)

    @r.get("/plex/playlists")
    async def plex_playlists(request: Request) -> list[dict[str, Any]]:
        return await src(request, "plex").playlists()

    @r.get("/plex/recent")
    async def plex_recent(request: Request, section: str | None = None, size: int = 30) -> list[dict[str, Any]]:
        return await src(request, "plex").recent(section, max(1, min(size, 100)))

    @r.get("/plex/search")
    async def plex_search(request: Request, q: str) -> dict[str, Any]:
        return await src(request, "plex").search(q)

    @r.get("/plex/art")
    async def plex_art(request: Request, path: str, size: int = 300) -> Response:
        data, ctype = await src(request, "plex").fetch_art(path, size)
        return Response(data, media_type=ctype, headers={"Cache-Control": "public, max-age=604800"})

    @r.post("/plex/play")
    async def plex_play(request: Request, body: PlexPlayBody) -> dict[str, Any]:
        await src(request, "plex").play_item(body.key, body.index, body.shuffle)
        return {"ok": True}

    @r.get("/plex/queue")
    async def plex_queue(request: Request) -> dict[str, Any]:
        return src(request, "plex").queue_view()

    @r.post("/plex/queue/{index}")
    async def plex_queue_play(request: Request, index: int) -> dict[str, Any]:
        await src(request, "plex").play_index(index)
        return {"ok": True}

    # -- cd
    @r.get("/cd")
    async def cd_info(request: Request) -> dict[str, Any]:
        return src(request, "cd").details()

    @r.post("/cd/play")
    async def cd_play(request: Request, body: CdPlayBody) -> dict[str, Any]:
        cd = src(request, "cd")
        # Reading a track can take a while; don't hold the HTTP request open for it.
        task = asyncio.create_task(cd.play_track(body.track))
        await asyncio.sleep(0.2)
        if task.done() and task.exception():
            raise task.exception()  # type: ignore[misc]
        return {"ok": True}

    @r.post("/cd/eject")
    async def cd_eject(request: Request) -> dict[str, Any]:
        await src(request, "cd").eject()
        return {"ok": True}

    @r.get("/cd/stream/{token}/{number}.wav", include_in_schema=False)
    async def cd_stream(request: Request, token: str, number: int) -> Response:
        """The disc as it is being read, for mpv on this machine. Waits for the drive."""
        if (request.client.host if request.client else "") not in ("127.0.0.1", "::1"):
            raise SourceError("Forbidden", 403)
        buf = src(request, "cd").buffer_for(token, number)
        if buf is None:
            raise SourceError("No such track on the disc in the drive", 404)
        size = buf.size
        rng = parse_range(request.headers.get("range"), size)
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        if request.headers.get("range") and rng is None:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        start, end = rng or (0, size - 1)
        headers["Content-Length"] = str(end - start + 1)
        if rng:
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
        return StreamingResponse(
            buf.stream(start, end), status_code=206 if rng else 200, media_type="audio/wav", headers=headers
        )

    # -- spotify (called by the librespot hook on this machine)
    @r.post("/spotify/event")
    async def spotify_event(request: Request) -> dict[str, Any]:
        c = ctx(request)
        client_host = request.client.host if request.client else ""
        if client_host not in ("127.0.0.1", "::1") or not secrets.compare_digest(
            request.headers.get("X-JPlay-Hook", ""), c.hook_token
        ):
            raise SourceError("Forbidden", 403)
        payload = await request.json()
        await src(request, "spotify").handle_event({str(k): str(v) for k, v in payload.items()})
        return {"ok": True}

    # -- bluetooth
    @r.get("/bluetooth")
    async def bt_info(request: Request) -> dict[str, Any]:
        return src(request, "bluetooth").details()

    @r.post("/bluetooth/discoverable")
    async def bt_discoverable(request: Request, body: DiscoverableBody) -> dict[str, Any]:
        bt = src(request, "bluetooth")
        await bt.set_discoverable(body.on)
        return bt.details()

    @r.post("/bluetooth/devices/{address}/{action}")
    async def bt_device(request: Request, address: str, action: str) -> dict[str, Any]:
        bt = src(request, "bluetooth")
        if action == "connect":
            await bt.connect(address)
        elif action == "disconnect":
            await bt.disconnect(address)
        else:
            raise SourceError("action must be connect or disconnect")
        return {"ok": True}

    @r.delete("/bluetooth/devices/{address}")
    async def bt_forget(request: Request, address: str) -> dict[str, Any]:
        await src(request, "bluetooth").forget(address)
        return {"ok": True}

    # -- version and updates (the separate updater does the work: deploy/update.sh). One version and one updater
    #    for everything on this Pi: the player, the display and the hub.
    def update_status(request: Request) -> dict[str, Any]:
        return request.app.state.updates.status()

    @r.get("/update")
    async def get_update(request: Request) -> dict[str, Any]:
        """Version, the newest release and its notes, and how an update is going."""
        return await asyncio.to_thread(update_status, request)

    @r.post("/update/check")
    async def update_check(request: Request) -> dict[str, Any]:
        try:
            await asyncio.to_thread(request.app.state.updates.check)
        except UpdateError as exc:
            raise SourceError(str(exc), 409) from exc
        return {"ok": True}

    @r.post("/update/install")
    async def update_install(request: Request, body: UpdateInstallBody | None = None) -> dict[str, Any]:
        """Update to the newest version (or a given "v1.6.0"). The updater restarts the player when done."""
        st = await asyncio.to_thread(update_status, request)
        target = (body.target if body else None) or st["target"]
        if not target:
            raise SourceError("Already up to date", 409)
        try:
            await asyncio.to_thread(request.app.state.updates.install, target)
        except UpdateError as exc:
            raise SourceError(str(exc), 409) from exc
        return {"ok": True, "target": target}

    @r.put("/update/settings")
    async def put_update_settings(request: Request, body: UpdateSettingsBody) -> dict[str, Any]:
        """Where updates come from (releases | main) and whether they install at night (off | night)."""
        request.app.state.updates.save_settings(body.channel, body.auto)
        return await asyncio.to_thread(update_status, request)

    # -- live events
    @r.websocket("/events")
    async def events(ws: WebSocket) -> None:
        c: AppContext = ws.app.state.ctx
        await ws.accept()
        queue = c.hub.subscribe()
        try:
            await ws.send_json({"type": "state", "data": c.manager.snapshot()})
            while True:
                msg = await queue.get()
                await ws.send_json(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            c.hub.unsubscribe(queue)

    return r


def app_version(app: FastAPI) -> str:
    """'1.6.0' from the git tags; the package version when git can't tell (a copy without .git)."""
    v = app.state.updates.version()["version"]
    return __version__ if v in ("unknown", "") else v


async def auto_update(app: FastAPI) -> None:
    """Every minute: install a waiting update at night when that's switched on and nothing plays.
    (The player does this for the whole Pi when it has one: it knows when music plays.)"""
    c: AppContext = app.state.ctx
    old = c.store.get("updates")   # settings from before 2.0 lived in the player's own state
    if old and not (system.state_root() / "update-settings.json").exists():
        app.state.updates.save_settings(old.get("channel"), old.get("auto"))
    while True:
        await asyncio.sleep(60)
        if system.primary_role() != "player":
            continue
        try:
            busy = c.manager.active_id is not None   # a source is active (playing, paused or waiting for a phone)
            target = await asyncio.to_thread(app.state.updates.auto_target, busy)
            if target:
                log.info("Installing %s automatically", target)
                await asyncio.to_thread(app.state.updates.install, target)
        except Exception as exc:
            log.warning("Automatic update: %s", exc)


def player_id(c: AppContext) -> str:
    """A random id made once and kept in the state file, so renaming the player keeps its identity."""
    pid = c.store.get("player_id")
    if not pid:
        pid = secrets.token_hex(8)
        c.store.set("player_id", pid)
    return pid


def create_app(config: Config) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = build_context(config)
        c.output = Output(c)
        sources = [RadioSource(c), PlexSource(c), CdSource(c), SpotifySource(c), BluetoothSource(c)]
        c.manager = SourceManager(c, sources)
        app.state.ctx = c
        app.state.updates = system.updates()
        await c.manager.start()
        announcer = Announcer(config["server"]["name"], int(config["server"]["port"]),
                              {"id": player_id(c), "version": app_version(app), "path": "/api/v1"})
        await announcer.start()
        updater = asyncio.create_task(auto_update(app), name="auto-update")
        log.info("JPlay ready on port %s (state in %s)", config["server"]["port"], c.state_dir)
        try:
            yield
        finally:
            updater.cancel()
            await announcer.shutdown()
            await c.manager.shutdown()
            with contextlib.suppress(Exception):
                await c.http.aclose()

    app = FastAPI(title="JPlay", version="0.1.0", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/v1/openapi.json", redoc_url=None)

    @app.exception_handler(SourceError)
    async def source_error(_: Request, exc: SourceError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    app.include_router(api_router())
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
