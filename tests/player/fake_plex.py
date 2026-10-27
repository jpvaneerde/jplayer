"""A tiny fake Plex Media Server: just enough XML for python-plexapi and JPlay's Plex source.

Run standalone to try the UI without a real server:
    python -m tests.player.fake_plex --port 32400 --audio-dir /path/with/wav/files
"""
from __future__ import annotations

import argparse
import struct
import zlib
from pathlib import Path
from xml.sax.saxutils import quoteattr

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response

ARTISTS = {
    100: {"title": "Miles Davis"},
    200: {"title": "Bill Evans Trio"},
    300: {"title": "Nina Simone"},
}
ALBUMS = {
    101: {"artist": 100, "title": "Kind of Blue", "year": 1959, "added": 1700000300},
    102: {"artist": 100, "title": "Sketches of Spain", "year": 1960, "added": 1700000200},
    201: {"artist": 200, "title": "Waltz for Debby", "year": 1962, "added": 1700000100},
    301: {"artist": 300, "title": "Pastel Blues", "year": 1965, "added": 1700000400},
}
TRACK_NAMES = {
    101: ["So What", "Freddie Freeloader", "Blue in Green"],
    102: ["Concierto de Aranjuez", "Will o' the Wisp"],
    201: ["My Foolish Heart", "Waltz for Debby", "Detour Ahead"],
    301: ["Be My Husband", "Nobody Knows You When You're Down and Out", "Sinnerman"],
}
TRACKS = {}
for album_id, names in TRACK_NAMES.items():
    for i, name in enumerate(names, start=1):
        TRACKS[album_id * 10 + i] = {"album": album_id, "index": i, "title": name}
PLAYLISTS = {500: {"title": "Late night", "tracks": [1013, 2011, 3013, 1011]}}

PNG_CACHE: dict[str, bytes] = {}


def png(color: tuple[int, int, int], size: int = 64) -> bytes:
    key = f"{color}{size}"
    if key not in PNG_CACHE:
        raw = b"".join(b"\x00" + bytes(color) * size for _ in range(size))

        def chunk(t: bytes, d: bytes) -> bytes:
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

        PNG_CACHE[key] = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )
    return PNG_CACHE[key]


def attrs(**kw) -> str:
    return " ".join(f"{k}={quoteattr(str(v))}" for k, v in kw.items() if v is not None)


def artist_xml(aid: int) -> str:
    return f"<Directory {attrs(ratingKey=aid, key=f'/library/metadata/{aid}/children', type='artist', title=ARTISTS[aid]['title'], thumb=f'/library/metadata/{aid}/thumb/1', librarySectionID=1, librarySectionKey='/library/sections/1')}/>"


def album_xml(bid: int) -> str:
    a = ALBUMS[bid]
    return f"<Directory {attrs(ratingKey=bid, key=f'/library/metadata/{bid}/children', type='album', title=a['title'], parentTitle=ARTISTS[a['artist']]['title'], parentRatingKey=a['artist'], year=a['year'], addedAt=a['added'], thumb=f'/library/metadata/{bid}/thumb/1', librarySectionID=1, leafCount=len(TRACK_NAMES[bid]))}/>"


def track_xml(tid: int) -> str:
    t = TRACKS[tid]
    a = ALBUMS[t["album"]]
    return (
        f"<Track {attrs(ratingKey=tid, key=f'/library/metadata/{tid}', type='track', title=t['title'], index=t['index'], parentTitle=a['title'], parentRatingKey=t['album'], parentThumb=f'/library/metadata/{t['album']}/thumb/1', grandparentTitle=ARTISTS[a['artist']]['title'], grandparentRatingKey=a['artist'], duration=8000, librarySectionID=1)}>"
        f"<Media {attrs(id=tid, duration=8000, audioCodec='flac', bitrate=1000, audioChannels=2, container='flac')}>"
        f"<Part {attrs(id=tid, key=f'/library/parts/{tid}/1/file.wav', duration=8000, file=f'/music/{tid}.flac', container='wav')}/>"
        "</Media></Track>"
    )


def playlist_xml(pid: int) -> str:
    p = PLAYLISTS[pid]
    return f"<Playlist {attrs(ratingKey=pid, key=f'/playlists/{pid}/items', type='playlist', playlistType='audio', title=p['title'], leafCount=len(p['tracks']), duration=8000 * len(p['tracks']), composite=f'/playlists/{pid}/composite/1', smart=0)}/>"


def container(body: list[str], total: int | None = None, **extra) -> Response:
    xml = f"<?xml version='1.0' encoding='utf-8'?><MediaContainer {attrs(size=len(body), totalSize=total, **extra)}>{''.join(body)}</MediaContainer>"
    return Response(xml, media_type="application/xml")


def page(req: Request, items: list[str]) -> Response:
    start = int(req.query_params.get("X-Plex-Container-Start", req.headers.get("X-Plex-Container-Start", 0)))
    size = int(req.query_params.get("X-Plex-Container-Size", req.headers.get("X-Plex-Container-Size", 10_000)))
    return container(items[start:start + size], total=len(items), offset=start)


def create_app(audio_dir: Path) -> FastAPI:
    app = FastAPI()
    app.state.scrobbles = []

    @app.get("/")
    async def root():
        return container([], friendlyName="Fake Plex", machineIdentifier="fake-plex-0001", version="1.40.0.0")

    @app.get("/identity")
    async def identity():
        return container([], machineIdentifier="fake-plex-0001", version="1.40.0.0")

    @app.get("/library")
    async def library():
        return container(["<Directory key='sections' title='Library Sections'/>"], title1="Plex Library")

    @app.get("/library/sections")
    async def sections():
        return container([f"<Directory {attrs(key=1, type='artist', title='Music', agent='tv.plex.agents.music', uuid='sec-1', allowSync=1)}/>"])

    @app.get("/library/sections/1/all")
    async def section_all(req: Request):
        kind = req.query_params.get("type")
        sort = req.query_params.get("sort", "titleSort")
        if kind == "8":
            ids = sorted(ARTISTS, key=lambda i: ARTISTS[i]["title"])
            return page(req, [artist_xml(i) for i in ids])
        if kind == "9":
            if sort.startswith("addedAt"):
                ids = sorted(ALBUMS, key=lambda i: -ALBUMS[i]["added"])
            else:
                ids = sorted(ALBUMS, key=lambda i: ALBUMS[i]["title"])
            return page(req, [album_xml(i) for i in ids])
        return container([])

    @app.get("/library/metadata/{rid}")
    async def metadata(rid: int):
        if rid in ARTISTS:
            return container([artist_xml(rid)])
        if rid in ALBUMS:
            return container([album_xml(rid)])
        if rid in TRACKS:
            return container([track_xml(rid)])
        if rid in PLAYLISTS:  # real Plex also serves playlists as metadata items
            return container([playlist_xml(rid)])
        return Response(status_code=404)

    @app.get("/library/metadata/{rid}/children")
    async def children(rid: int):
        if rid in ARTISTS:
            return container([album_xml(b) for b, a in ALBUMS.items() if a["artist"] == rid])
        if rid in ALBUMS:
            return container([track_xml(t) for t, x in TRACKS.items() if x["album"] == rid])
        return Response(status_code=404)

    @app.get("/library/metadata/{rid}/allLeaves")
    async def leaves(rid: int):
        return container([track_xml(t) for t, x in TRACKS.items() if ALBUMS[x["album"]]["artist"] == rid])

    @app.get("/playlists")
    async def playlists():
        return container([playlist_xml(p) for p in PLAYLISTS])

    @app.get("/playlists/{pid}")
    async def playlist(pid: int):
        return container([playlist_xml(pid)])

    @app.get("/playlists/{pid}/items")
    async def playlist_items(pid: int):
        return container([track_xml(t) for t in PLAYLISTS[pid]["tracks"]])

    @app.get("/hubs/search")
    async def search(req: Request):
        q = req.query_params.get("query", "").lower()
        want = req.query_params.get("mediatype")
        hubs = []
        groups = {
            "artist": [artist_xml(i) for i, a in ARTISTS.items() if q in a["title"].lower()],
            "album": [album_xml(i) for i, a in ALBUMS.items() if q in a["title"].lower()],
            "track": [track_xml(i) for i, t in TRACKS.items() if q in t["title"].lower()],
        }
        for kind, items in groups.items():
            if want and want != kind:
                continue
            hubs.append(f"<Hub {attrs(type=kind, hubIdentifier=kind, size=len(items), title=kind.title())}>{''.join(items)}</Hub>")
        return container(hubs)

    @app.get("/:/scrobble")
    async def scrobble(key: str):
        app.state.scrobbles.append(key)
        return container([])

    @app.get("/photo/:/transcode")
    async def transcode(url: str = ""):
        seed = sum(map(ord, url)) % 255
        return Response(png((40 + seed % 120, 60 + seed % 90, 80 + seed % 70)), media_type="image/png")

    @app.get("/library/parts/{tid}/{_n}/file.wav")
    async def part(tid: int, _n: str):
        files = sorted(audio_dir.glob("*.wav"))
        return FileResponse(files[tid % len(files)], media_type="audio/wav")

    return app


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=32400)
    ap.add_argument("--audio-dir", type=Path, required=True)
    a = ap.parse_args()
    uvicorn.run(create_app(a.audio_dir), host="127.0.0.1", port=a.port, log_level="warning")
