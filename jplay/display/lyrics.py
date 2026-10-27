"""Song lyrics on the display. Many songs have *synced* lyrics (a time per line), others only the text.

Where they come from, in order:
1. The hub: the lyrics store for the whole house. It looks songs up on LRCLIB itself and keeps them, with
   corrections made on its Lyrics page (a song's timing, lyrics pasted by hand).
2. When there's no hub or it can't be reached: this display's own copy (lyrics.db), then LRCLIB directly.

lookup() answers {"found", "instrumental", "synced": [[seconds, line], ...] | None, "plain": [line, ...] | None,
"offset"}: offset (seconds) is the song's timing correction, added to the position before picking the line.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

import requests

from ..common import hub, lrclib
from ..common.lrclib import NOT_FOUND, cache_key

RETRY_MISS_AFTER = 3 * 86400       # a song without lyrics is asked again after 3 days
RETRY_FAILED_AFTER = 45            # seconds

_db_path: str | None = None
_lock = threading.Lock()
_failed: dict[str, float] = {}     # song -> when LRCLIB last failed for it (memory only, never stored)
fetch = lrclib.fetch               # tests replace this


def init(path: str) -> None:
    global _db_path
    _db_path = path
    with _connect() as db:
        db.execute("CREATE TABLE IF NOT EXISTS lyrics (key TEXT PRIMARY KEY, answer TEXT, found INTEGER, fetched_at REAL)")
        if db.execute("PRAGMA user_version").fetchone()[0] < lrclib.MATCHER_VERSION:
            db.execute("""DELETE FROM lyrics WHERE found = 0 OR answer LIKE '%"found": false%'""")
            db.execute(f"PRAGMA user_version = {lrclib.MATCHER_VERSION}")


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(_db_path, timeout=10)


def from_hub(base: str, artist: str, title: str, album: str = "", duration: float | None = None) -> dict:
    """Ask the hub (raises when it can't answer)."""
    params = {"artist": artist, "title": title, "album": album or ""}
    if duration:
        params["duration"] = round(duration)
    r = requests.get(base + "/api/lyrics", params=params, timeout=45, headers=lrclib.HEADERS)
    r.raise_for_status()
    ans = r.json()
    return {"found": bool(ans.get("found")), "instrumental": bool(ans.get("instrumental")),
            "synced": ans.get("synced") or None, "plain": ans.get("plain") or None,
            "offset": float(ans.get("offset") or 0)}


def _store(key: str, ans: dict) -> None:
    if _db_path:
        ans = {k: ans.get(k) for k in NOT_FOUND}
        with _lock, _connect() as db:
            db.execute("INSERT OR REPLACE INTO lyrics VALUES (?, ?, ?, ?)",
                       (key, json.dumps(ans), int(bool(ans["found"])), time.time()))


def lookup(artist: str, title: str, album: str = "", duration: float | None = None, hub_address: str | None = "") -> dict:
    """Lyrics for a song. hub_address: the hub set in Settings ("" = find it; None = don't ask a hub).

    Never raises. When LRCLIB couldn't be asked, the answer has "unavailable": True (nothing is stored) and the
    display asks again a minute later.
    """
    if not artist or not title:
        return dict(NOT_FOUND)
    key = cache_key(artist, title)
    base = hub.find(hub_address) if hub_address is not None else None
    if base:
        # Always ask the hub first, so a correction made there shows up; keep a copy for when it's away.
        try:
            ans = from_hub(base, artist, title, album, duration)
            _store(key, ans)
            return ans
        except Exception as e:
            hub.forget()
            print(f"Lyrics: the hub didn't answer ({type(e).__name__}: {e}); using this display's copy or LRCLIB")
    with _lock:
        if _db_path:
            with _connect() as db:
                row = db.execute("SELECT answer, found, fetched_at FROM lyrics WHERE key = ?", (key,)).fetchone()
            if row and (row[1] or time.time() - row[2] < RETRY_MISS_AFTER):
                return json.loads(row[0])
    if time.time() - _failed.get(key, 0) < RETRY_FAILED_AFTER:
        return {**NOT_FOUND, "unavailable": True}
    try:
        ans = fetch(artist, title, album, duration)
    except Exception as e:  # LRCLIB busy or unreachable: not stored; the display asks again in a minute
        print(f"Lyrics lookup failed for {artist} – {title}: {type(e).__name__}: {e}")
        _failed[key] = time.time()
        if len(_failed) > 500:
            _failed.clear()
        return {**NOT_FOUND, "unavailable": True}
    _failed.pop(key, None)
    _store(key, ans)
    return {k: ans.get(k) for k in NOT_FOUND}
