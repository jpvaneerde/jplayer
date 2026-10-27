"""The hub's SQLite database (/var/lib/jplay/hub/hub.db): plays, lyrics, rooms, units and settings.

One connection, used from worker threads under a lock (the web handlers call in with asyncio.to_thread).
Times are milliseconds since 1970, as in JPlay Records, so plays imported from it keep their times.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()
_depth = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Rooms: which one wins when several hear the same song, and which physical formats play there.
CREATE TABLE IF NOT EXISTS rooms (
  name     TEXT PRIMARY KEY,
  priority INTEGER NOT NULL DEFAULT 1,       -- higher wins when several rooms hear the same song
  formats  TEXT NOT NULL DEFAULT '[]'        -- ["vinyl","cd"]: a turntable / CD player stands here
);

-- Displays (and players) that report to the hub.
CREATE TABLE IF NOT EXISTS units (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  room         TEXT NOT NULL,
  kind         TEXT NOT NULL DEFAULT 'display',
  address      TEXT NOT NULL DEFAULT '',
  version      TEXT NOT NULL DEFAULT '',
  first_seen_at INTEGER NOT NULL,
  last_seen_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS spin_events (
  id          TEXT PRIMARY KEY,              -- uuid from the unit (resending is harmless)
  unit_id     TEXT NOT NULL,
  room        TEXT NOT NULL,
  type        TEXT NOT NULL,                 -- mic | mic_end | room
  ts          INTEGER NOT NULL,              -- when it happened (ms)
  data        TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  processed   INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS spin_events_todo ON spin_events (processed, received_at);
CREATE INDEX IF NOT EXISTS spin_events_room ON spin_events (room, type, ts);

CREATE TABLE IF NOT EXISTS plays (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  room         TEXT NOT NULL,
  kind         TEXT NOT NULL,                -- physical | streamed | heard
  detail       TEXT NOT NULL DEFAULT '',     -- vinyl | cd | radio | plex | spotify | bluetooth | …
  station      TEXT,
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER,
  title        TEXT NOT NULL,
  artist       TEXT NOT NULL DEFAULT '',
  album        TEXT NOT NULL DEFAULT '',
  cover        TEXT,                         -- the song's cover (Shazam, iTunes)
  song_key     TEXT NOT NULL,
  instance_id  INTEGER,                      -- your copy in JPlay Records, for physical plays
  release_id   INTEGER,
  record       TEXT,                         -- that copy as JPlay Records described it: {"artist","title","year","cover"}
  confidence   TEXT NOT NULL DEFAULT 'mic',  -- mic | metadata | mic+metadata | likely | ambiguous | manual
  heard_by     TEXT NOT NULL DEFAULT '[]',   -- rooms whose microphone heard it
  alternatives TEXT NOT NULL DEFAULT '[]',   -- other copies it could have been: [{"id","artist","title","medium","cover"}]
  mic_ids      TEXT NOT NULL DEFAULT '[]',
  note         TEXT,                         -- why it matched (or didn't)
  via          TEXT,                         -- 'player': the room's JPlay player said what it was
  origin       TEXT NOT NULL DEFAULT 'hub',  -- hub | records (imported from JPlay Records) | display (imported history)
  import_id    TEXT,                         -- where an imported play came from (importing twice adds nothing)
  created_at   INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS plays_import ON plays (import_id) WHERE import_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS plays_started ON plays (started_at);
CREATE INDEX IF NOT EXISTS plays_key ON plays (song_key, started_at);
CREATE INDEX IF NOT EXISTS plays_instance ON plays (instance_id, started_at);

-- Listening time per room and source (radio sessions are counted even when no song is known).
CREATE TABLE IF NOT EXISTS room_sessions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  room         TEXT NOT NULL,
  mode         TEXT NOT NULL,                -- radio | plex | cd | spotify | bluetooth
  station      TEXT NOT NULL DEFAULT '',
  started_at   INTEGER NOT NULL,
  ended_at     INTEGER,
  last_seen_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS room_sessions_room ON room_sessions (room, ended_at);

CREATE TABLE IF NOT EXISTS lyrics (
  key           TEXT PRIMARY KEY,              -- normalised "artist|title" (lrclib.cache_key)
  artist        TEXT NOT NULL,
  title         TEXT NOT NULL,
  album         TEXT NOT NULL DEFAULT '',
  duration      REAL,                          -- seconds, when known
  found         INTEGER NOT NULL DEFAULT 0,
  instrumental  INTEGER NOT NULL DEFAULT 0,
  synced        TEXT,                          -- JSON [[seconds, line], ...]
  plain         TEXT,                          -- JSON [line, ...]
  offset        REAL NOT NULL DEFAULT 0,       -- timing correction for this song (seconds)
  source        TEXT NOT NULL DEFAULT 'lrclib',-- lrclib | manual (manual entries are never overwritten)
  lrclib_id     INTEGER,
  fetched_at    INTEGER NOT NULL,              -- when LRCLIB was last asked (ms)
  asked_count   INTEGER NOT NULL DEFAULT 0,
  last_asked_at INTEGER
);
"""


def now() -> int:
    return int(time.time() * 1000)


def init(path: str) -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute("PRAGMA foreign_keys = ON")
        _conn.executescript(SCHEMA)


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _c() -> sqlite3.Connection:
    if _conn is None:
        raise RuntimeError("The hub's database isn't open")
    return _conn


def all(sql: str, *params: Any) -> list[dict]:  # noqa: A001 - same name as in JPlay Records
    with _lock:
        return [dict(r) for r in _c().execute(sql, params).fetchall()]


def get(sql: str, *params: Any) -> dict | None:
    with _lock:
        r = _c().execute(sql, params).fetchone()
        return dict(r) if r else None


def run(sql: str, *params: Any) -> sqlite3.Cursor:
    with _lock:
        return _c().execute(sql, params)


@contextmanager
def tx():
    """A transaction (nested calls join the outer one)."""
    with _lock:
        depth = getattr(_depth, "n", 0)
        if depth == 0:
            _c().execute("BEGIN IMMEDIATE")
        _depth.n = depth + 1
        try:
            yield
        except BaseException:
            _depth.n = depth
            if depth == 0:
                _c().execute("ROLLBACK")
            raise
        _depth.n = depth
        if depth == 0:
            _c().execute("COMMIT")


def setting(key: str, default: Any = None) -> Any:
    r = get("SELECT value FROM settings WHERE key = ?", key)
    return json.loads(r["value"]) if r else default


def set_setting(key: str, value: Any) -> None:
    run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        key, json.dumps(value))
