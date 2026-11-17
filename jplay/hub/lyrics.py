"""Lyrics for the whole house. The displays ask here first; a song nobody asked for before is looked up on
LRCLIB once and kept, found or not. A correction made on the hub's Lyrics page (the timing, or lyrics pasted by
hand) reaches every display.

Which lyrics a song gets: pasted by hand, else an .lrc from your music share (lyrics_share.py, the newest file
wins), else LRCLIB.

An answer: {"found", "instrumental", "synced": [[seconds, line(, words)], ...] | None, "plain": [line, ...] | None, "offset"}
offset (seconds) is added to the song's position before choosing the line: + shows lines later, - earlier.
"""
from __future__ import annotations

import json
import logging
import threading

from ..common import lrclib
from . import db
from .db import now

log = logging.getLogger(__name__)
RETRY_MISS_MS = 3 * 86_400_000     # a song without lyrics is looked up again after 3 days
RETRY_ERROR_MS = 45_000            # LRCLIB busy or unreachable: not again for this song for 45 seconds

_failed: dict[str, int] = {}       # LRCLIB errors per song (memory only, never stored)
_inflight: dict[str, threading.Event] = {}
_guard = threading.Lock()
fetch = lrclib.fetch               # tests replace this


def init() -> None:
    """Songs not found with older matching rules are looked up again."""
    if (db.setting("lyrics_matcher", 0) or 0) < lrclib.MATCHER_VERSION:
        db.run("DELETE FROM lyrics WHERE found = 0 AND source <> 'manual'")
        db.set_setting("lyrics_matcher", lrclib.MATCHER_VERSION)


def _answer(r: dict) -> dict:
    return {"found": bool(r["found"]), "instrumental": bool(r["instrumental"]),
            "synced": json.loads(r["synced"]) if r["synced"] else None,
            "plain": json.loads(r["plain"]) if r["plain"] else None, "offset": r["offset"] or 0}


def _row(key: str) -> dict | None:
    return db.get("SELECT * FROM lyrics WHERE key = ?", key)


def lookup(artist: str, title: str, album: str = "", duration: float | None = None) -> dict:
    """The lyrics of a song: from the store, or looked up on LRCLIB and stored. Never raises.
    "unavailable": True = LRCLIB couldn't be asked (and nothing was stored)."""
    artist, title, album = (str(x or "").strip()[:300] for x in (artist, title, album))
    if not artist or not title:
        return dict(lrclib.NOT_FOUND)
    key = lrclib.cache_key(artist, title)
    if not all(key.split("|")):
        return dict(lrclib.NOT_FOUND)
    db.run("UPDATE lyrics SET asked_count = asked_count + 1, last_asked_at = ? WHERE key = ?", now(), key)
    row = _row(key)
    if row and (row["found"] or row["source"] != "lrclib" or now() - row["fetched_at"] < RETRY_MISS_MS):
        return _answer(row)
    if now() - _failed.get(key, 0) < RETRY_ERROR_MS:
        return _answer(row) if row else {**lrclib.NOT_FOUND, "unavailable": True}

    # Two displays asking at the same moment: one LRCLIB lookup, both get its answer.
    with _guard:
        waiting = _inflight.get(key)
        if waiting is None:
            _inflight[key] = threading.Event()
    if waiting is not None:
        waiting.wait(60)
        row = _row(key)
        return _answer(row) if row else {**lrclib.NOT_FOUND, "unavailable": True}
    try:
        try:
            ans = fetch(artist, title, album, duration)
        except Exception as e:
            log.warning("Lyrics: LRCLIB lookup failed for %s – %s: %s", artist, title, e)
            _failed[key] = now()
            if len(_failed) > 2000:
                _failed.pop(next(iter(_failed)))
            return _answer(row) if row else {**lrclib.NOT_FOUND, "unavailable": True}
        _failed.pop(key, None)
        db.run("""INSERT INTO lyrics (key, artist, title, album, duration, found, instrumental, synced, plain, lrclib_id,
                    fetched_at, asked_count, last_asked_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                  ON CONFLICT(key) DO UPDATE SET album = excluded.album, duration = excluded.duration,
                    found = excluded.found, instrumental = excluded.instrumental, synced = excluded.synced,
                    plain = excluded.plain, lrclib_id = excluded.lrclib_id, fetched_at = excluded.fetched_at
                  WHERE lyrics.source = 'lrclib'""",
               key, artist, title, album, duration, int(ans["found"]), int(bool(ans.get("instrumental"))),
               json.dumps(ans["synced"]) if ans.get("synced") else None,
               json.dumps(ans["plain"]) if ans.get("plain") else None, ans.get("lrclib_id"), now(), now())
        stored = _row(key)
        return _answer(stored) if stored else {k: ans.get(k) for k in lrclib.NOT_FOUND}
    finally:
        with _guard:
            ev = _inflight.pop(key, None)
        if ev:
            ev.set()


# ---------------------------------------------------------------- the Lyrics page
def songs(q: str = "", only: str = "", limit: int = 100, offset: int = 0) -> dict:
    where, params = [], []
    if q:
        where.append("(artist LIKE ? OR title LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if only == "missing":
        where.append("found = 0")
    elif only == "plain":
        where.append("found = 1 AND synced IS NULL")
    elif only == "corrected":
        where.append("(offset <> 0 OR source = 'manual')")
    elif only == "file":
        where.append("source = 'file'")
    w = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.get(f"SELECT COUNT(*) AS n FROM lyrics {w}", *params)["n"]
    rows = db.all(f"""SELECT key, artist, title, album, duration, found, instrumental, synced IS NOT NULL AS synced,
                        synced LIKE '%]]]%' AS words, offset, source, file_path, asked_count, last_asked_at, fetched_at
                      FROM lyrics {w} ORDER BY COALESCE(last_asked_at, fetched_at) DESC LIMIT ? OFFSET ?""",
                  *params, limit, offset)
    for r in rows:
        r["found"], r["instrumental"], r["synced"] = bool(r["found"]), bool(r["instrumental"]), bool(r["synced"])
        r["words"] = bool(r["words"])
    return {"total": total, "songs": rows}


def song(key: str) -> dict | None:
    r = _row(key)
    return {**_answer(r), **{k: r[k] for k in ("key", "artist", "title", "album", "duration", "source", "file_path")}} \
        if r else None


def set_offset(key: str, offset: float) -> bool:
    return db.run("UPDATE lyrics SET offset = ? WHERE key = ?", max(-60.0, min(60.0, float(offset))), key).rowcount > 0


def set_text(key: str, text: str) -> bool:
    """Lyrics pasted by hand: LRC ("[01:02.50] line") for synced lyrics, or plain lines. Never overwritten by LRCLIB."""
    synced = lrclib.parse_lrc(text) or None
    plain = [line.rstrip() for line in text.strip().splitlines()] if not synced else [e[1] for e in synced]
    found = bool(synced or any(plain))
    return db.run("UPDATE lyrics SET synced = ?, plain = ?, found = ?, instrumental = 0, source = 'manual', fetched_at = ? "
                  "WHERE key = ?", json.dumps(synced) if synced else None, json.dumps(plain) if found else None,
                  int(found), now(), key).rowcount > 0


def forget(key: str) -> None:
    """Look this song up on LRCLIB again the next time it plays (the offset goes too). A song with an .lrc on the
    music share gets that again at the next scan."""
    db.run("DELETE FROM lyrics WHERE key = ?", key)


def stats() -> dict:
    return db.get("SELECT COUNT(*) AS songs, COALESCE(SUM(found), 0) AS found, "
                  "COALESCE(SUM(synced IS NOT NULL), 0) AS synced, COALESCE(SUM(source = 'file'), 0) AS files "
                  "FROM lyrics")


# ---------------------------------------------------------------- .lrc files on the music share
def apply_files() -> dict:
    """Bring the songs in line with the .lrc files found on the share: per song the newest usable file wins
    (unless lyrics were pasted by hand). A song whose files are all gone goes back to LRCLIB.
    Answers {"updated": n, "removed": n}."""
    winners = db.all("""SELECT f.* FROM lyric_files f
                        WHERE f.key IS NOT NULL AND f.error IS NULL AND NOT EXISTS (
                          SELECT 1 FROM lyric_files g WHERE g.key = f.key AND g.error IS NULL
                            AND (g.mtime > f.mtime OR (g.mtime = f.mtime AND g.path > f.path)))""")
    current = {r["key"]: r for r in db.all("SELECT key, source, file_path, file_mtime FROM lyrics")}
    updated = removed = 0
    with db.tx():
        for w in winners:
            c = current.get(w["key"])
            if c and (c["source"] == "manual" or
                      (c["source"] == "file" and c["file_path"] == w["path"] and c["file_mtime"] == w["mtime"])):
                continue
            # New lyrics: a timing correction made for the old ones wouldn't fit, so it starts at 0 again
            db.run("""INSERT INTO lyrics (key, artist, title, album, duration, found, instrumental, synced, plain, offset,
                        source, fetched_at, file_path, file_mtime)
                      VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?, 0, 'file', ?, ?, ?)
                      ON CONFLICT(key) DO UPDATE SET artist = excluded.artist, title = excluded.title,
                        album = excluded.album, duration = COALESCE(excluded.duration, lyrics.duration), found = 1,
                        instrumental = 0, synced = excluded.synced, plain = excluded.plain, offset = 0, source = 'file',
                        lrclib_id = NULL, fetched_at = excluded.fetched_at, file_path = excluded.file_path,
                        file_mtime = excluded.file_mtime
                      WHERE lyrics.source <> 'manual'""",
                   w["key"], w["artist"], w["title"], w["album"], w["duration"], w["synced"], w["plain"], now(),
                   w["path"], w["mtime"])
            updated += 1
        keep = {w["key"] for w in winners}
        for key, c in current.items():
            if c["source"] == "file" and key not in keep:
                db.run("DELETE FROM lyrics WHERE key = ? AND source = 'file'", key)
                removed += 1
    return {"updated": updated, "removed": removed}


def store_imported(row: dict) -> bool:
    """A song from JPlay Records' lyrics store (keeps its timing correction and hand-made lyrics)."""
    key = lrclib.cache_key(row["artist"], row["title"])
    if not all(key.split("|")):
        return False
    cur = db.run("""INSERT INTO lyrics (key, artist, title, album, duration, found, instrumental, synced, plain, offset,
                      source, lrclib_id, fetched_at, asked_count, last_asked_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(key) DO NOTHING""",
                 key, row["artist"], row["title"], row.get("album") or "", row.get("duration"), int(bool(row.get("found"))),
                 int(bool(row.get("instrumental"))), row.get("synced"), row.get("plain"), row.get("offset") or 0,
                 row.get("source") or "lrclib", row.get("lrclib_id"), row.get("fetched_at") or now(),
                 row.get("asked_count") or 0, row.get("last_asked_at"))
    return cur.rowcount > 0
