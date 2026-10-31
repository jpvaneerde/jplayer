"""Pictures from plain http:// addresses (a player's covers, radio station logos), passed on by this app.

On the secure address (https://lounge-display.home.jplay.cloud) a browser warns about, or blocks, pictures from
http:// addresses. The pages ask for /api/img?u=<address> instead; only pictures are passed on (nothing else),
at most MAX_BYTES, and kept for a while so a logo isn't fetched again for every poll.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response

MAX_BYTES = 5 * 1024 * 1024
KEEP = 300                       # pictures kept in memory
KEEP_FOR = 3600                  # seconds
_cache: "OrderedDict[str, tuple[float, bytes, str]]" = OrderedDict()


async def fetch(url: str, client: httpx.AsyncClient | None = None) -> tuple[bytes, str]:
    if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
        raise HTTPException(400, "Not a picture address")
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < KEEP_FOR:
        _cache.move_to_end(url)
        return hit[1], hit[2]
    own = client is None
    c = client or httpx.AsyncClient(timeout=8, follow_redirects=True)
    try:
        async with c.stream("GET", url) as r:
            kind = r.headers.get("content-type", "").split(";")[0].strip()
            if r.status_code != 200 or not kind.startswith("image/"):
                raise HTTPException(404, "No picture there")
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise HTTPException(413, "Picture too large")
    except httpx.HTTPError:
        raise HTTPException(404, "Picture not reachable")
    finally:
        if own:
            await c.aclose()
    _cache[url] = (time.time(), body, kind)
    while len(_cache) > KEEP:
        _cache.popitem(last=False)
    return body, kind


def add_route(app: FastAPI, path: str = "/api/img") -> None:
    @app.get(path)
    async def image(u: str):
        body, kind = await fetch(u)
        return Response(body, media_type=kind, headers={"Cache-Control": "public, max-age=86400"})
