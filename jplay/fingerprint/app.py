"""The fingerprint service's API (see __init__.py). Settings come from the environment:

  DATA_DIR=/data          the index (Panako's database and cache, the library, links)
  MUSIC_DIR=/music        your music, read-only
  API_TOKEN=...           asked as "Authorization: Bearer ..." for recognize/index/rebuild (none: open)
  INDEX_TIME=02:30        the nightly look for new files (local time, TZ)
  INDEX_ON_START=1        also look when the service starts
  PANAKO_PROCESSORS=2     threads for fingerprinting
  MIN_SCORE=5             matches with fewer matching prints than this are ignored
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response

from .. import __version__
from .library import Library
from .panako import Panako, PanakoError

log = logging.getLogger(__name__)
COVER_FILES = ("cover.jpg", "folder.jpg", "front.jpg", "cover.png", "folder.png", "front.png", "albumart.jpg")


def cover_of(path: Path) -> tuple[bytes, str] | None:
    """The picture in the file, else cover.jpg / folder.jpg / ... next to it."""
    try:
        import mutagen

        f = mutagen.File(str(path))
        pics = list(getattr(f, "pictures", None) or [])
        if pics:
            p = next((x for x in pics if getattr(x, "type", 0) == 3), pics[0])   # 3: front cover
            return p.data, p.mime or "image/jpeg"
        tags = getattr(f, "tags", None)
        if tags is not None:
            for k in list(tags.keys()):
                if str(k).startswith("APIC"):                   # mp3
                    return tags[k].data, tags[k].mime or "image/jpeg"
            if "covr" in tags and tags["covr"]:                  # m4a
                c = tags["covr"][0]
                return bytes(c), "image/png" if getattr(c, "imageformat", 0) == 14 else "image/jpeg"
    except Exception:
        pass
    folder = path.parent
    try:
        names = {n.lower(): n for n in os.listdir(folder)}
    except OSError:
        return None
    for want in COVER_FILES:
        if want in names:
            data = (folder / names[want]).read_bytes()
            return data, "image/png" if want.endswith(".png") else "image/jpeg"
    return None


def create_app(data_dir: str | None = None, music_dir: str | None = None, token: str | None = None,
               schedule: bool = True) -> FastAPI:
    data = Path(data_dir or os.environ.get("DATA_DIR", "/data"))
    music = Path(music_dir or os.environ.get("MUSIC_DIR", "/music"))
    token = os.environ.get("API_TOKEN", "") if token is None else token
    min_score = int(os.environ.get("MIN_SCORE", "5"))
    panako = Panako(data, processors=int(os.environ.get("PANAKO_PROCESSORS", "2")))
    lib = Library(data, music, panako)
    queries = asyncio.Semaphore(int(os.environ.get("PARALLEL_QUERIES", "2")))
    stats = {"asked": 0, "found": 0, "last_ms": None, "last_error": ""}

    async def run_index() -> None:
        try:
            result = await asyncio.to_thread(lib.index)
            log.info("Index: %s", result)
        except RuntimeError as e:
            log.info("Index: %s", e)
        except Exception as e:
            log.warning("Index failed: %s: %s", type(e).__name__, e)

    async def nightly() -> None:
        if os.environ.get("INDEX_ON_START", "1") == "1":
            await run_index()
        hh, mm = (int(x) for x in os.environ.get("INDEX_TIME", "02:30").split(":"))
        while True:
            now = datetime.now()
            nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep((nxt - now).total_seconds())
            await run_index()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(nightly()) if schedule else None
        yield
        if task:
            task.cancel()

    app = FastAPI(title="JPlay fingerprint", version=__version__, lifespan=lifespan, docs_url="/api/docs")
    app.state.library = lib

    def check(request: Request) -> None:
        if not token:
            return
        got = request.headers.get("authorization", "")
        if not hmac.compare_digest(got.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Wrong or missing token")

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/api/status")
    async def status():
        return {"version": __version__, "music_dir": str(music), "music_found": music.is_dir(),
                **lib.counts(), "index": lib.run, "recognize": stats}

    @app.post("/api/index")
    async def index_now(request: Request):
        check(request)
        if lib.run["running"]:
            return {"started": False, "message": "Indexing is already running"}
        asyncio.create_task(run_index())
        return {"started": True}

    @app.post("/api/rebuild")
    async def rebuild(request: Request):
        """Start the index again (tidies away removed songs; the prints come from the cache, so it's quick)."""
        check(request)
        try:
            await asyncio.to_thread(lib.rebuild)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        asyncio.create_task(run_index())
        return {"started": True}

    @app.post("/api/recognize")
    async def recognize(request: Request):
        """A WAV clip (the body) -> {"found": bool, "song": {...}, "offset": seconds into the song, ...}."""
        check(request)
        body = await request.body()
        if len(body) < 1000:
            raise HTTPException(400, "Send a WAV clip as the body")
        if len(body) > 20 * 1024 * 1024:
            raise HTTPException(413, "Clip too long")
        stats["asked"] += 1
        started = time.monotonic()
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=os.environ.get("TMPDIR")) as f:
            f.write(body)
            f.flush()
            async with queries:
                try:
                    matches = await asyncio.to_thread(panako.query, f.name)
                except PanakoError as e:
                    stats["last_error"] = str(e)
                    raise HTTPException(503, str(e))
        stats["last_ms"] = round((time.monotonic() - started) * 1000)
        for m in matches:
            if m.score < min_score:
                break
            song = lib.song(m.id)
            if not song or song["gone"]:
                continue
            stats["found"] += 1
            return {"found": True, "offset": round(m.offset, 1), "score": m.score, "time_factor": m.time_factor,
                    "elapsed_ms": stats["last_ms"],
                    "song": {k: song[k] for k in ("id", "title", "artist", "album", "albumartist", "year", "track",
                                                  "duration", "path")} | {"cover": f"/api/cover/{song['id']}"}}
        return {"found": False, "elapsed_ms": stats["last_ms"]}

    @app.get("/api/cover/{sid}")
    async def cover(sid: int):
        song = lib.song(sid)
        if not song:
            raise HTTPException(404, "No such song")
        pic = await asyncio.to_thread(cover_of, music / song["path"])
        if not pic:
            raise HTTPException(404, "No cover")
        return Response(pic[0], media_type=pic[1], headers={"Cache-Control": "public, max-age=86400"})

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
