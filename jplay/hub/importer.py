"""Bringing in what the old setup knew (Settings → Import on the hub). Each import can run again: what's
already there is skipped.

  JPlay Records     plays (with which of your records it was), rooms (priority, formats) and the lyrics store,
                    with its address and a listener token (GET /api/spins/export)
  a display         the songs an old JPlaying Now's microphone heard (its History), for plays JPlay Records
                    never got (GET /api/history)
  a player          the radio favourites of one player copied to another (also an old player-jplay)
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

import requests

from . import db, lyrics, records, records_sync, spins
from .db import now

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "JPlay hub (https://github.com/jpvaneerde/jplayer)"}
PAGE = 500

status: dict = {"running": False, "what": "", "message": "", "done": None, "error": "", "counts": {}}
_lock = threading.Lock()


class ImportError_(Exception):
    pass


def _get(url: str, params: dict | None = None, token: str = "", timeout: float = 60) -> object:
    try:
        r = requests.get(url, params=params, timeout=timeout,
                         headers={**HEADERS, **({"Authorization": f"Bearer {token}"} if token else {})})
    except Exception as e:
        raise ImportError_(f"Can't reach {url.split('/api')[0]}: {type(e).__name__}") from e
    if r.status_code == 401:
        raise ImportError_("JPlay Records doesn't know this token")
    if r.status_code == 404:
        raise ImportError_(f"{url.split('/api')[0]} doesn't offer this (too old? update it first)")
    if not r.ok:
        raise ImportError_(f"HTTP {r.status_code} from {url}")
    return r.json()


def _base(address: str, port: int) -> str:
    a = address.strip().rstrip("/")
    if not a.startswith("http"):
        a = "http://" + a
    if a.count(":") < 2:
        a += f":{port}"
    return a


def run_in_background(what: str, fn, *args) -> bool:
    """Start an import; False when one is running already."""
    with _lock:
        if status["running"]:
            return False
        status.update(running=True, what=what, message="Starting", done=None, error="", counts={})

    def job():
        try:
            fn(*args)
            status["message"] = "Done"
        except Exception as e:
            log.exception("Import %s failed", what)
            status["error"] = str(e)
            status["message"] = "Failed"
        finally:
            status.update(running=False, done=time.time())
    threading.Thread(target=job, name=f"import-{what}", daemon=True).start()
    return True


def _count(name: str, n: int = 1) -> None:
    status["counts"][name] = status["counts"].get(name, 0) + n


# ---------------------------------------------------------------- JPlay Records
def from_records(url: str, token: str) -> None:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    status["message"] = "Rooms"
    data = _get(f"{url}/api/spins/export", {"part": "rooms"}, token)
    for r in data.get("rooms", []):
        existing = db.get("SELECT name FROM rooms WHERE name = ?", r["room"])
        if not existing:
            spins.save_room(r["room"], r.get("priority", 1), r.get("formats", []))
            _count("rooms")

    status["message"] = "Plays"
    after = 0
    while True:
        data = _get(f"{url}/api/spins/export", {"part": "plays", "after": after, "limit": PAGE}, token)
        rows = data.get("plays", [])
        if not rows:
            break
        with db.tx():
            for p in rows:
                _store_records_play(url, p)
        after = rows[-1]["id"]
        status["message"] = f"Plays: {status['counts'].get('plays', 0)} so far"
        if len(rows) < PAGE:
            break

    status["message"] = "Lyrics"
    after_key = ""
    while True:
        data = _get(f"{url}/api/spins/export", {"part": "lyrics", "after": after_key, "limit": PAGE}, token)
        rows = data.get("lyrics", [])
        if not rows:
            break
        with db.tx():
            for r in rows:
                if lyrics.store_imported(r):
                    _count("lyrics")
        after_key = rows[-1]["key"]
        if len(rows) < PAGE:
            break

    # The same address and token keep matching new plays to your records from now on (when not set up yet)
    if not records.enabled():
        db.set_setting("records_url", url)
        db.set_setting("records_token", token)
        _count("records connected")


def _abs(url: str, path: str | None) -> str | None:
    if not path:
        return None
    return path if path.startswith("http") else url + "/" + path.lstrip("/")


def _store_records_play(url: str, p: dict) -> None:
    record = p.get("record")
    if record:
        record = {"artist": record.get("artist"), "title": record.get("title"), "year": record.get("year"),
                  "cover": _abs(url, record.get("cover"))}
    alts = [{**a, "cover": _abs(url, a.get("cover"))} for a in p.get("alternatives") or []]
    cur = db.run("""INSERT OR IGNORE INTO plays (room, kind, detail, station, started_at, ended_at, title, artist, album,
                      cover, song_key, instance_id, release_id, record, confidence, heard_by, alternatives, mic_ids, note,
                      via, origin, import_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?, 'records', ?, ?)""",
                 p["room"], p["kind"], p.get("detail") or "", p.get("station"), p["started_at"], p.get("ended_at"),
                 p["title"], p.get("artist") or "", p.get("album") or "", p.get("cover"),
                 spins.song_key(p.get("artist"), p["title"]) or p["title"].lower(),
                 p.get("instance_id") if record else None, p.get("release_id"),
                 json.dumps(record) if record else None, p.get("confidence") or "mic",
                 json.dumps(p.get("heard_by") or []), json.dumps(alts), p.get("note"), p.get("via"),
                 f"records:{p['id']}", p.get("created_at") or now())
    if cur.rowcount:
        _count("plays")
        records_sync.mark_imported(cur.lastrowid)       # JPlay Records has it already
        db.run("INSERT OR IGNORE INTO rooms (name) VALUES (?)", p["room"])


# ---------------------------------------------------------------- an old JPlaying Now's history
def from_display(address: str, room: str) -> None:
    base = _base(address, 5000)
    room = room.strip()
    if not room:
        raise ImportError_("Say which room that display is in")
    db.run("INSERT OR IGNORE INTO rooms (name) VALUES (?)", room)
    offset = 0
    new_ids = []
    while True:
        data = _get(f"{base}/api/history", {"limit": PAGE, "offset": offset})
        rows = data.get("plays", [])
        with db.tx():
            for h in rows:
                pid = _store_display_play(base, room, h)
                if pid:
                    new_ids.append(pid)
        offset += len(rows)
        status["message"] = f"History: {offset} of {data.get('total', '?')} songs read"
        if len(rows) < PAGE:
            break
    if new_ids and records.enabled():
        status["message"] = "Matching them to your records"
        for i in range(0, len(new_ids), 200):
            chunk = new_ids[i:i + 200]
            rows = db.all(f"SELECT * FROM plays WHERE id IN ({','.join('?' * len(chunk))})", *chunk)
            _count("matched to your records", spins.rematch_rows(rows))


def _store_display_play(base: str, room: str, h: dict) -> int | None:
    try:
        ts = int(datetime.strptime(h["ts"], "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
    except (KeyError, ValueError):
        return None
    title, artist = (h.get("title") or "").strip(), (h.get("artist") or "").strip()
    key = spins.song_key(artist, title)
    if not key:
        return None
    # Already known (JPlay Records got it then, or this history was imported before): skip
    if spins.similar_play(ts, artist, title, 5 * 60_000):
        _count("already there")
        return None
    cover = h.get("coverart") or None
    cur = db.run("""INSERT OR IGNORE INTO plays (room, kind, detail, started_at, title, artist, album, cover, song_key,
                      confidence, heard_by, note, origin, import_id, created_at)
                    VALUES (?, 'heard', '', ?, ?, ?, ?, ?, ?, 'mic', ?, ?, 'display', ?, ?)""",
                 room, ts, title, artist, h.get("album") or "", cover if cover and cover.startswith("http") else None,
                 key, json.dumps([room]), "From the old JPlaying Now's history", f"display:{base}:{h.get('id')}", now())
    if cur.rowcount:
        _count("plays")
        return cur.lastrowid
    return None


# ---------------------------------------------------------------- radio favourites from one player to another
def favourites(source: str, target: str) -> None:
    src, dst = _base(source, 8080), _base(target, 8080)
    favs = _get(f"{src}/api/v1/radio/favourites")
    have = {f.get("uuid") for f in _get(f"{dst}/api/v1/radio/favourites")}
    for f in favs:
        if f.get("uuid") in have:
            _count("already there")
            continue
        r = requests.post(f"{dst}/api/v1/radio/favourites", json=f, timeout=15, headers=HEADERS)
        if not r.ok:
            raise ImportError_(f"{dst} refused '{f.get('name')}': HTTP {r.status_code}")
        _count("favourites")
