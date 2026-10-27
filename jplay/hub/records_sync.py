"""Keeping JPlay Records' Spins up to date (when JPlay Records is connected and this is switched on).

The hub is where plays are decided; JPlay Records gets a copy of each, so its Spins pages and its record pages
("played 12 times, last on Sunday") keep counting. Every minute the hub sends the plays that are new or changed
since it last sent them (POST /api/spins/hub-plays on JPlay Records, with the listener token) and the plays
deleted on the hub. A play is only marked as sent once JPlay Records has it, so nothing is lost while it's away.

Plays that came from JPlay Records (imported) are sent back only when they are corrected on the hub, and then
update that same play there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid

from . import db, records
from .db import now

log = logging.getLogger(__name__)
BATCH = 200
RECENT_DAYS = 30            # plays this recent are checked for changes every time (ended, corrected, merged)
FIELDS = ("room", "kind", "detail", "station", "started_at", "ended_at", "title", "artist", "album", "cover",
          "instance_id", "release_id", "confidence", "heard_by", "alternatives", "note", "via")

status: dict = {"last_ok": None, "last_error": "", "sent": 0, "waiting": None}


def enabled() -> bool:
    return records.enabled() and bool(db.setting("records_sync", True))


def hub_key() -> str:
    """This hub's own id, so its plays are told apart from anything else in JPlay Records."""
    k = db.setting("hub_key")
    if not k:
        k = uuid.uuid4().hex[:12]
        db.set_setting("hub_key", k)
    return k


def hub_id(play_id: int) -> str:
    return f"{hub_key()}:{play_id}"


def records_id(row: dict) -> int | None:
    """The play's id in JPlay Records when it was imported from there."""
    imp = row.get("import_id") or ""
    return int(imp.split(":", 1)[1]) if imp.startswith("records:") and imp.split(":", 1)[1].isdigit() else None


def fingerprint(row: dict) -> str:
    return hashlib.sha1(json.dumps([row.get(f) for f in FIELDS], default=str).encode()).hexdigest()[:16]


def payload(row: dict) -> dict:
    alts = [a.get("id") for a in json.loads(row.get("alternatives") or "[]") if isinstance(a, dict) and a.get("id")]
    return {
        "hub_id": hub_id(row["id"]), "records_id": records_id(row),
        **{f: row.get(f) for f in FIELDS if f not in ("heard_by", "alternatives")},
        "heard_by": json.loads(row.get("heard_by") or "[]"), "alternatives": alts,
    }


def changed(play_id: int) -> None:
    """A play corrected by hand: send it again (also when it is older than RECENT_DAYS)."""
    db.run("UPDATE plays SET synced_hash = 'changed' WHERE id = ? AND synced_hash IS NOT NULL", play_id)


def forget(rows: list[dict]) -> None:
    """Plays about to be deleted on the hub: delete them in JPlay Records too (if it has them)."""
    for r in rows:
        if r.get("synced_hash"):
            db.run("INSERT OR REPLACE INTO records_deleted (hub_id, records_id, deleted_at) VALUES (?, ?, ?)",
                   hub_id(r["id"]), records_id(r), now())


def mark_imported(play_id: int) -> None:
    """A play just imported from JPlay Records: it has it already."""
    r = db.get("SELECT * FROM plays WHERE id = ?", play_id)
    if r:
        db.run("UPDATE plays SET synced_hash = ? WHERE id = ?", fingerprint(r), play_id)


def pending() -> tuple[list[dict], list[dict]]:
    """(plays to send, deletions to send)."""
    since = now() - RECENT_DAYS * 86_400_000
    # New plays, plays corrected by hand (marked 'changed'), and recent plays (they still end, merge, match)
    rows = db.all("SELECT * FROM plays WHERE synced_hash IS NULL OR synced_hash = 'changed' OR started_at >= ? "
                  "ORDER BY started_at", since)
    todo = [r for r in rows if r["synced_hash"] != fingerprint(r)]
    deleted = db.all("SELECT * FROM records_deleted ORDER BY deleted_at")
    return todo, deleted


def sync_once() -> int:
    """Send what JPlay Records hasn't got yet. Returns how many plays (and deletions) it took."""
    if not enabled():
        status["waiting"] = None
        return 0
    todo, deleted = pending()
    status["waiting"] = len(todo) + len(deleted)
    done = 0
    try:
        for i in range(0, max(len(todo), 1), BATCH):
            chunk = todo[i:i + BATCH]
            dels = deleted if i == 0 else []
            if not chunk and not dels:
                break
            body = {"hub": hub_key(), "plays": [payload(r) for r in chunk],
                    "deleted": [{"hub_id": d["hub_id"], "records_id": d["records_id"]} for d in dels]}
            records._post("/api/spins/hub-plays", body, timeout=60)
            with db.tx():
                for r in chunk:
                    # Only if it didn't change again while it was on its way
                    db.run("UPDATE plays SET synced_hash = ? WHERE id = ? AND synced_hash IS ?",
                           fingerprint(r), r["id"], r["synced_hash"])
                for d in dels:
                    db.run("DELETE FROM records_deleted WHERE hub_id = ?", d["hub_id"])
            done += len(chunk) + len(dels)
    except records.RecordsError as e:
        status["last_error"] = str(e)
        log.info("Sending plays to JPlay Records: %s", e)
        return done
    status.update(last_ok=time.time(), last_error="", sent=status["sent"] + done, waiting=0)
    return done
