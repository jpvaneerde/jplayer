"""JPlay Records (optional): which of your records or CDs a song came from.

Without it the hub still keeps every play; it just can't tell which copy of yours was played: songs the
microphone hears while no JPlay player plays are "heard" (a record, the TV, a phone…), and a CD in a JPlay
player is "a CD" without a specific copy.

With it (Settings → JPlay Records: its address and a listener token from JPlay Records → Settings → Listeners),
the hub asks it for each such song: POST /api/spins/match. Covers of your records are fetched once through
the hub (GET /covers/... with the same token) and kept in /var/lib/jplay/hub/covers.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import db

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "JPlay hub (https://github.com/jpvaneerde/jplayer)"}
BATCH = 50


class RecordsError(Exception):
    """JPlay Records couldn't be asked (not the same as "not in your collection")."""


def config() -> tuple[str, str] | None:
    url = str(db.setting("records_url", "") or "").strip().rstrip("/")
    token = str(db.setting("records_token", "") or "").strip()
    return (url, token) if url and token else None


def enabled() -> bool:
    return config() is not None


def _post(path: str, body: dict, timeout: float = 30) -> dict:
    cfg = config()
    if not cfg:
        raise RecordsError("JPlay Records isn't set up")
    try:
        r = requests.post(cfg[0] + path, json=body, timeout=timeout,
                          headers={**HEADERS, "Authorization": f"Bearer {cfg[1]}"})
    except Exception as e:
        raise RecordsError(f"{type(e).__name__}: {e}") from e
    if r.status_code == 401:
        raise RecordsError("JPlay Records doesn't know this token (JPlay Records → Settings → Listeners)")
    if r.status_code == 404:
        raise RecordsError("This JPlay Records is too old for the hub: update it")
    if not r.ok:
        raise RecordsError(f"HTTP {r.status_code}: {r.text[:200]}")
    return r.json()


def match_many(songs: list[dict]) -> list[dict]:
    """songs: [{"artist", "title", "album", "formats": ["vinyl", "cd"], "player_cd": bool}]

    -> one result per song: {"match": None | {"instanceId", "releaseId", "medium", "confidence", "note",
    "alternatives": [{"id", "artist", "title", "medium", "cover"}]}, "note": str,
    "record": None | {"id", "artist", "title", "year", "cover"}}. Raises RecordsError.
    """
    out: list[dict] = []
    for i in range(0, len(songs), BATCH):
        chunk = songs[i:i + BATCH]
        ans = _post("/api/spins/match", {"songs": [
            {"artist": s.get("artist", ""), "title": s.get("title", ""), "album": s.get("album", ""),
             "formats": s.get("formats") or [], "playerCd": bool(s.get("player_cd"))} for s in chunk]})
        results = ans.get("results")
        if not isinstance(results, list) or len(results) != len(chunk):
            raise RecordsError("JPlay Records gave an unexpected answer")
        out += [_absolute(r, config()[0]) for r in results]
    return out


def _absolute(res: dict, base: str) -> dict:
    """Covers as full addresses (they are paths on JPlay Records), so the hub can fetch them later."""
    def fix(x):
        if isinstance(x, dict) and x.get("cover") and str(x["cover"]).startswith("/"):
            x["cover"] = base + x["cover"]
        return x
    fix(res.get("record"))
    for a in (res.get("match") or {}).get("alternatives") or []:
        fix(a)
    return res


def match(artist: str, title: str, album: str, formats: list[str], player_cd: bool = False) -> dict:
    return match_many([{"artist": artist, "title": title, "album": album, "formats": formats, "player_cd": player_cd}])[0]


def test() -> dict:
    """Settings → JPlay Records → Test: does the address and token work?"""
    try:
        match("Test", "Test", "", [])
        return {"ok": True}
    except RecordsError as e:
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------- covers of your records
def cover_path(covers_dir: Path, url: str) -> Path | None:
    """The cover of one of your records, fetched from JPlay Records once and kept. None when it can't be had."""
    cfg = config()
    if not cfg or not url:
        return None
    # Only covers from the configured JPlay Records (never fetch anything else with its token)
    full = url if url.startswith("http") else cfg[0] + "/" + url.lstrip("/")
    if urlparse(full).netloc != urlparse(cfg[0]).netloc or not urlparse(full).path.startswith("/covers/"):
        return None
    name = hashlib.sha1(full.encode()).hexdigest() + Path(urlparse(full).path).suffix[:6]
    path = covers_dir / name
    if path.exists():
        return path
    try:
        r = requests.get(full, timeout=15, headers={**HEADERS, "Authorization": f"Bearer {cfg[1]}"})
        if not r.ok or not r.headers.get("content-type", "").startswith("image/"):
            return None
        covers_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(r.content)
        tmp.replace(path)
        return path
    except Exception as e:
        log.info("Cover %s: %s", full, e)
        return None
