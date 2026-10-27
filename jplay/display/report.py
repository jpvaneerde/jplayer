"""Telling the hub what this room hears and plays (JPlay Spins).

Events are queued in a local outbox (next to the history) and sent in batches, so nothing is lost while the
network or the hub is down. The hub decides what each event means (which room, a record or a stream); this
side only reports:

  mic      the microphone recognised a song (title, artist, album, cover, loudness)
  mic_end  that song is no longer heard
  room     this room's JPlay player changed state: play/pause/stop, the source (radio, plex, cd, spotify,
           bluetooth), the radio station and the song if known (for radio also songs recognised from the
           stream); repeated every few minutes as a heartbeat
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid

import requests

from ..common import hub

DB_PATH: str | None = None
HEARTBEAT_SECONDS = 300
KEEP_SECONDS = 14 * 86400      # unsent events older than this are dropped
MAX_QUEUED = 50000
_lock = threading.Lock()
_room = {"sig": None, "sent": 0.0}
status = {"queued": 0, "last_ok": None, "last_error": "", "last_attempt": None, "hub": None}


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init(path: str) -> None:
    global DB_PATH
    DB_PATH = path
    with _connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS spin_outbox (
                          id TEXT PRIMARY KEY, body TEXT NOT NULL, created REAL NOT NULL,
                          attempts INTEGER NOT NULL DEFAULT 0)""")
    _count()


def _count() -> None:
    with _connect() as conn:
        status["queued"] = conn.execute("SELECT COUNT(*) FROM spin_outbox").fetchone()[0]


def enabled(cfg: dict) -> bool:
    return bool(cfg.get("report_plays", True) and str(cfg.get("room") or "").strip())


def queue(event_type: str, ts: float, data: dict) -> str | None:
    """Add an event to the outbox; returns its id."""
    if DB_PATH is None:
        return None
    eid = str(uuid.uuid4())
    body = json.dumps({"id": eid, "type": event_type, "ts": ts, "data": data})
    with _lock, _connect() as conn:
        conn.execute("INSERT INTO spin_outbox (id, body, created) VALUES (?, ?, ?)", (eid, body, time.time()))
    status["queued"] += 1
    return eid


def room_update(data: dict) -> None:
    """This room's player state (players.spins_room_data): queued when it changes, and as a heartbeat."""
    sig = json.dumps(data, sort_keys=True)
    now = time.time()
    if sig != _room["sig"] or now - _room["sent"] >= HEARTBEAT_SECONDS:
        queue("room", now, data)
        _room.update(sig=sig, sent=now)


def send_pending(cfg: dict, unit: dict, batch: int = 200) -> int:
    """Send queued events to the hub. Returns the number sent; problems end up in status['last_error']."""
    if DB_PATH is None or not enabled(cfg):
        return 0
    with _connect() as conn:
        conn.execute("DELETE FROM spin_outbox WHERE created < ?", (time.time() - KEEP_SECONDS,))
        conn.execute("DELETE FROM spin_outbox WHERE id NOT IN (SELECT id FROM spin_outbox ORDER BY created DESC LIMIT ?)",
                     (MAX_QUEUED,))
        rows = conn.execute("SELECT id, body FROM spin_outbox ORDER BY created LIMIT ?", (batch,)).fetchall()
    if not rows:
        status["queued"] = 0
        return 0
    base = hub.find(str(cfg.get("hub") or ""))
    status["hub"] = base
    if not base:
        status["last_error"] = "No hub found on the network (set its address in Settings, or make one unit the hub)"
        return 0
    status["last_attempt"] = time.time()
    try:
        r = requests.post(base + "/api/events", timeout=15,
                          json={"unit": unit, "events": [json.loads(row["body"]) for row in rows]})
    except Exception as e:
        hub.forget()
        status["last_error"] = f"The hub at {base} didn't answer ({type(e).__name__})"
        return 0
    if not r.ok:
        status["last_error"] = f"The hub said HTTP {r.status_code}: {r.text[:200]}"
        with _connect() as conn:
            conn.executemany("UPDATE spin_outbox SET attempts = attempts + 1 WHERE id = ?", [(row["id"],) for row in rows])
        return 0
    with _lock, _connect() as conn:
        conn.executemany("DELETE FROM spin_outbox WHERE id = ?", [(row["id"],) for row in rows])
    status["last_ok"] = time.time()
    status["last_error"] = ""
    _count()
    return len(rows)
