"""JPlay Spins: what was played, where, and from which source (the same rules JPlay Records used).

The displays (one per room) send raw events:
  mic      the microphone recognised a song (Shazam): title, artist, album, cover, loudness
  mic_end  that song is no longer heard
  room     this room's JPlay player changed state; repeated every few minutes as a heartbeat: play/pause/stop,
           the source (radio, plex, cd, spotify, bluetooth), the station and the song if known (radio songs the
           display recognised from the stream included)
A worker turns them into plays a little later (so every room's report is in):
  1. A song heard in several rooms at about the same time is ONE play.
  2. If a room that heard it has its player playing: that source. A CD in the JPlay player is a physical play
     of a CD; radio, Plex, Spotify and Bluetooth are streamed plays.
  3. Otherwise: the highest-priority room that heard it, and, when JPlay Records is connected and the song is on
     a record or CD you own in a format that room can play, a physical play of that copy; else "heard"
     (a record, the TV, a phone…).
  4. The player's own song info adds plays the microphone missed; for radio the microphone wins when they
     disagree.
Physical plays of the same copy (or album) in a row form a "spin" (the record being put on).
"""
from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from . import db, records, records_sync
from .db import now

log = logging.getLogger(__name__)

SETTLE_MS = 45_000            # wait this long for the other rooms' reports before deciding
SAME_SONG_MS = 180_000        # same song within this window = one play
RADIO_CONFLICT_MS = 150_000
ROOM_STALE_MS = 12 * 60_000   # no room heartbeat for this long = display/player gone
SPIN_GAP_MS = 20 * 60_000     # tracks of the same copy closer than this = one spin
TRACK_GUESS_MS = 4 * 60_000   # assumed length when a play has no end
FORMAT_ORDER = ["vinyl", "cd", "cassette"]
EVENT_TYPES = ("mic", "mic_end", "room")

NOTE_NO_PLAYER = "Not from a JPlay player: a record, a CD, the TV…"
NOTE_PLAYER_CD = "CD in the JPlay player"
NOTE_RECORDS_AWAY = "JPlay Records couldn't be asked; matched again later"


# ---------------------------------------------------------------- rooms and units
def rooms() -> list[dict]:
    rows = db.all("SELECT name, priority, formats FROM rooms ORDER BY priority DESC, name")
    return [{"name": r["name"], "priority": r["priority"], "formats": json.loads(r["formats"])} for r in rows]


def room_info() -> dict[str, dict]:
    out = {}
    for r in rooms():
        out[r["name"]] = {"priority": r["priority"],
                          "formats": sorted(r["formats"], key=lambda f: FORMAT_ORDER.index(f) if f in FORMAT_ORDER else 9)}
    return out


def save_room(name: str, priority: int, formats: list[str], old_name: str | None = None) -> None:
    name = name.strip()[:40]
    if not name:
        raise ValueError("Give the room a name")
    formats = [f for f in dict.fromkeys(str(x).lower() for x in formats) if f in FORMAT_ORDER]
    priority = max(0, min(100, int(priority)))
    with db.tx():
        if old_name and old_name != name:
            # Renaming a room renames it everywhere
            for table in ("plays", "room_sessions", "spin_events", "units"):
                db.run(f"UPDATE {table} SET room = ? WHERE room = ?", name, old_name)
            db.run("DELETE FROM rooms WHERE name = ?", old_name)
        db.run("INSERT INTO rooms (name, priority, formats) VALUES (?, ?, ?) "
               "ON CONFLICT(name) DO UPDATE SET priority = excluded.priority, formats = excluded.formats",
               name, priority, json.dumps(formats))


def room_name(name: str) -> str:
    """The room as it is already known ("study" from one display, "Study" from another: one room)."""
    name = str(name or "").strip()[:40]
    r = db.get("SELECT name FROM rooms WHERE lower(name) = lower(?)", name)
    return r["name"] if r else name


def delete_room(name: str) -> None:
    db.run("DELETE FROM rooms WHERE name = ?", name)


def units() -> list[dict]:
    t = now()
    return [{**u, "online": t - u["last_seen_at"] < ROOM_STALE_MS}
            for u in db.all("SELECT * FROM units ORDER BY room, name")]


def forget_unit(unit_id: str) -> None:
    db.run("DELETE FROM units WHERE id = ?", unit_id)


# ---------------------------------------------------------------- ingest
def ingest(unit: dict, events: list, address: str = "") -> int:
    """Events from a display: {"id", "type", "ts", "data"}. Returns how many were new."""
    uid = str(unit.get("id") or "").strip()[:64]
    room = room_name(str(unit.get("room") or ""))
    if not uid or not room:
        raise ValueError("Say which unit this is and its room")
    t = now()
    n = 0
    with db.tx():
        db.run("INSERT OR IGNORE INTO rooms (name) VALUES (?)", room)   # a new room: priority 1, no formats
        db.run("""INSERT INTO units (id, name, room, kind, address, version, first_seen_at, last_seen_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                  ON CONFLICT(id) DO UPDATE SET name = excluded.name, room = excluded.room, kind = excluded.kind,
                    address = excluded.address, version = excluded.version, last_seen_at = excluded.last_seen_at""",
               uid, str(unit.get("name") or uid)[:60], room, str(unit.get("kind") or "display")[:20],
               str(unit.get("address") or address)[:200], str(unit.get("version") or "")[:40], t, t)
        for e in (events or [])[:500]:
            if not isinstance(e, dict):
                continue
            eid, etype = str(e.get("id") or ""), str(e.get("type") or "")
            try:
                ts = float(e.get("ts"))
            except (TypeError, ValueError):
                continue
            if not eid or len(eid) > 64 or etype not in EVENT_TYPES:
                continue
            if ts < 1e12:
                ts *= 1000                       # seconds from Python
            ts = min(int(round(ts)), t + 60_000)
            data = json.dumps(e.get("data") if isinstance(e.get("data"), dict) else {})
            if len(data) > 8000:
                continue
            cur = db.run("INSERT OR IGNORE INTO spin_events (id, unit_id, room, type, ts, data, received_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", eid, uid, room, etype, ts, data, t)
            n += cur.rowcount
    return n


# ---------------------------------------------------------------- helpers
def norm(s: Any) -> str:
    """'Purple Rain (2015 Remaster)' -> 'purple rain'; accents and punctuation ignored."""
    s = unicodedata.normalize("NFD", str(s or "").lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\s+-\s+.*$", "", s)
    s = re.sub(r"\s+(feat\.?|ft\.?|featuring)\s+.*$", "", s)
    s = re.sub(r"^the\s+", "", s)
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w]+", " ", s)       # letters of every script stay (JPlay Records kept only a-z and 0-9)
    return s.replace("_", " ").strip()


def song_key(artist: Any, title: Any) -> str:
    t = norm(title)
    return f"{norm(artist)}|{t}" if t else ""


def words_of(s: str) -> list[str]:
    return [w for w in s.split(" ") if len(w) > 1 and w != "and"]


def artist_similarity(x: str, y: str) -> int:
    """3 = same, 2 = one contains the other, 1 = most words shared, 0 = different."""
    if not x or not y:
        return 0
    if x == y:
        return 3
    if x in y or y in x:
        return 2
    a, b = set(words_of(x)), set(words_of(y))
    if not a or not b:
        return 0
    return 1 if len(a & b) / max(len(a), len(b)) >= 0.6 else 0


def same_song(a: dict, b: dict) -> bool:
    """The same song, even when one source spells the artist differently (radio text vs the microphone)."""
    t = norm(a["title"])
    if not t or t != norm(b["title"]):
        return False
    x, y = norm(a["artist"]), norm(b["artist"])
    return not x or not y or artist_similarity(x, y) >= 2


def cleaner_artist(x: str, y: str) -> str:
    """Of two spellings of the same artist, the clean one (the shorter, when one contains the other)."""
    if not x:
        return y
    if not y:
        return x
    return y if len(norm(y)) < len(norm(x)) and norm(y) in norm(x) else x


def similar_play(ts: int, artist: str, title: str, window: int) -> dict | None:
    rows = db.all("SELECT * FROM plays WHERE started_at BETWEEN ? AND ? ORDER BY ABS(started_at - ?)",
                  ts - window, ts + window, ts)
    return next((r for r in rows if same_song(r, {"artist": artist, "title": title})), None)


def room_state_at(room: str, ts: int) -> dict | None:
    """What this room's player was doing at time ts (None = unknown: no recent report)."""
    ev = db.get("SELECT data, ts FROM spin_events WHERE room = ? AND type = 'room' AND ts <= ? ORDER BY ts DESC LIMIT 1",
                room, ts + 5_000)
    if not ev or ts - ev["ts"] > ROOM_STALE_MS:
        return None
    d = json.loads(ev["data"])
    return {k: str(d.get(k) or "") for k in ("state", "mode", "station", "title", "artist", "album")}


# ---------------------------------------------------------------- matching your records (JPlay Records)
def _collection(artist: str, title: str, album: str, formats: list[str], player_cd: bool = False) -> dict:
    """{"match", "note", "record", "away"}: away = JPlay Records couldn't be asked."""
    if not records.enabled():
        return {"match": None, "note": NOTE_PLAYER_CD if player_cd else NOTE_NO_PLAYER, "record": None, "away": False}
    try:
        res = records.match(artist, title, album, formats, player_cd)
        return {"match": res.get("match"), "note": res.get("note") or "", "record": res.get("record"), "away": False}
    except records.RecordsError as e:
        log.info("JPlay Records: %s", e)
        return {"match": None, "note": NOTE_RECORDS_AWAY, "record": None, "away": True}


def attribute(heard: list[str], ts: int, artist: str, title: str, album: str) -> dict:
    info = room_info()
    prio = lambda r: info.get(r, {}).get("priority", 0)  # noqa: E731
    by_prio = sorted(dict.fromkeys(heard), key=lambda r: -prio(r))
    # A room that heard it has its player playing: that source.
    for r in by_prio:
        s = room_state_at(r, ts)
        if not s or s["state"] != "play":
            continue
        if s["mode"] == "cd":
            c = _collection(artist, title, album, ["cd"], player_cd=True)
            return {"room": r, "kind": "physical", "detail": "cd", "station": None, "collection": c, "via": "player"}
        return {"room": r, "kind": "streamed", "detail": s["mode"] or "stream", "station": s["station"] or None,
                "collection": None, "via": None}
    room = by_prio[0]
    formats = info.get(room, {}).get("formats", [])
    c = _collection(artist, title, album, formats)
    if c["match"]:
        return {"room": room, "kind": "physical", "detail": c["match"].get("medium") or "", "station": None,
                "collection": c, "via": None}
    return {"room": room, "kind": "heard", "detail": "", "station": None, "collection": c, "via": None}


def _match_columns(c: dict | None) -> dict:
    """The plays columns that say which copy it was."""
    m = (c or {}).get("match") or None
    return {
        "instance_id": m.get("instanceId") if m else None,
        "release_id": m.get("releaseId") if m else None,
        "record": json.dumps(c["record"]) if m and m.get("instanceId") and (c or {}).get("record") else None,
        "alternatives": json.dumps((m or {}).get("alternatives") or []),
        "note": (c or {}).get("note") or (m or {}).get("note") or None,
        "confidence": (m or {}).get("confidence"),
    }


# ---------------------------------------------------------------- processing
def process_events(at: int | None = None) -> int:
    at = now() if at is None else at
    todo = db.all("SELECT id, room, type, ts, data FROM spin_events WHERE processed = 0 AND received_at <= ? "
                  "ORDER BY ts, received_at", at - SETTLE_MS)
    for ev in todo:
        try:
            with db.tx():
                d = json.loads(ev["data"])
                if ev["type"] == "room":
                    apply_room(ev["room"], ev["ts"], d)
                elif ev["type"] == "mic":
                    apply_mic(ev["id"], ev["room"], ev["ts"], d)
                elif ev["type"] == "mic_end":
                    apply_mic_end(ev["ts"], d)
                db.run("UPDATE spin_events SET processed = 1 WHERE id = ?", ev["id"])
        except Exception as e:
            log.exception("Spins: event %s (%s) failed: %s", ev["id"], ev["type"], e)
            db.run("UPDATE spin_events SET processed = 2 WHERE id = ?", ev["id"])
    # A display or player that went silent: close its session at the last sign of life.
    db.run("UPDATE room_sessions SET ended_at = last_seen_at WHERE ended_at IS NULL AND last_seen_at < ?",
           at - ROOM_STALE_MS)
    # Raw events are only needed for a while.
    db.run("DELETE FROM spin_events WHERE processed <> 0 AND ts < ?", at - 30 * 86_400_000)
    return len(todo)


def apply_room(room: str, ts: int, d: dict) -> None:
    state = str(d.get("state") or "")
    mode = str(d.get("mode") or "") or "file"
    station = str(d.get("station") or "")
    playing = state == "play"
    open_ = db.get("SELECT id, mode, station, last_seen_at FROM room_sessions WHERE room = ? AND ended_at IS NULL "
                   "ORDER BY id DESC LIMIT 1", room)
    same = bool(open_) and playing and open_["mode"] == mode and open_["station"] == station
    if open_ and not same:
        db.run("UPDATE room_sessions SET ended_at = ? WHERE id = ?", max(ts, open_["last_seen_at"]), open_["id"])
    if playing and not same:
        db.run("INSERT INTO room_sessions (room, mode, station, started_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
               room, mode, station, ts, ts)
    elif same:
        db.run("UPDATE room_sessions SET last_seen_at = MAX(last_seen_at, ?) WHERE id = ?", ts, open_["id"])

    # The player's own song info: a play the microphone may not have caught.
    if not playing:
        end_open_metadata_play(room, ts)
        return
    artist = str(d.get("artist") or "").strip()
    title = str(d.get("title") or "").strip()
    if mode == "radio":
        # Radio sends "Artist - Title" in the title, or a show name, slogan or ad.
        if not artist and " - " in title:
            artist, title = title.split(" - ", 1)
        if not artist or not title or re.search(r"https?:|www\.", title, re.I) or len(title) > 120 \
                or norm(title) == norm(station):
            end_open_metadata_play(room, ts)
            return
    key = song_key(artist, title)
    if not key:
        return
    cover = str(d.get("cover") or "") or None
    recent = db.get("SELECT * FROM plays WHERE room = ? AND song_key = ? AND started_at >= ? "
                    "ORDER BY started_at DESC LIMIT 1", room, key, ts - 15 * 60_000)
    if recent and cover and not recent["cover"]:
        db.run("UPDATE plays SET cover = ? WHERE id = ?", cover, recent["id"])
    if mode == "cd":
        player_cd_play(room, ts, recent, {"title": title, "artist": artist, "album": str(d.get("album") or ""), "key": key,
                                          "cover": cover})
        return
    if recent:
        # A heartbeat repeating the same song, or the microphone already logged it: just confirm.
        if recent["confidence"] != "manual" and (recent["kind"] != "streamed" or "metadata" not in recent["confidence"]):
            db.run("UPDATE plays SET kind = 'streamed', detail = ?, station = ?, instance_id = NULL, release_id = NULL, "
                   "record = NULL, confidence = 'mic+metadata', note = NULL WHERE id = ?", mode, station or None, recent["id"])
        return
    # The same song already logged under another spelling (the microphone, or another room's player): one play.
    similar = similar_play(ts, artist, title, 10 * 60_000)
    if similar:
        clean = cleaner_artist(similar["artist"], artist)
        if cover and not similar["cover"]:
            db.run("UPDATE plays SET cover = ? WHERE id = ?", cover, similar["id"])
        if similar["room"] == room and similar["confidence"] != "manual" and similar["kind"] != "physical":
            db.run("UPDATE plays SET kind = 'streamed', detail = ?, station = ?, instance_id = NULL, release_id = NULL, "
                   "record = NULL, confidence = ?, artist = ?, song_key = ?, note = NULL WHERE id = ?",
                   mode, station or None, "mic+metadata" if "mic" in similar["confidence"] else similar["confidence"],
                   clean, song_key(clean, similar["title"]), similar["id"])
        elif clean != similar["artist"] and similar["confidence"] != "manual":
            db.run("UPDATE plays SET artist = ?, song_key = ? WHERE id = ?", clean, song_key(clean, similar["title"]),
                   similar["id"])
        return
    if mode == "radio":
        # The microphone heard something else on this radio at the same moment: it is right, the metadata isn't.
        if db.get("SELECT id FROM plays WHERE room = ? AND confidence LIKE 'mic%' AND started_at BETWEEN ? AND ?",
                  room, ts - RADIO_CONFLICT_MS, ts + RADIO_CONFLICT_MS):
            return
    end_open_metadata_play(room, ts)
    db.run("""INSERT INTO plays (room, kind, detail, station, started_at, title, artist, album, cover, song_key, confidence,
                created_at) VALUES (?, 'streamed', ?, ?, ?, ?, ?, ?, ?, ?, 'metadata', ?)""",
           room, mode, station or None, ts, title, artist, str(d.get("album") or ""), cover, key, now())


def player_cd_play(room: str, ts: int, recent: dict | None, p: dict) -> None:
    """The room's JPlay player plays a CD track: a physical play of a CD (which copy: JPlay Records, if connected)."""
    if recent and recent["confidence"] == "manual":
        return
    if recent and recent["via"] == "player" and recent["kind"] == "physical":
        return                                   # heartbeat for the same track
    c = _collection(p["artist"], p["title"], p["album"], ["cd"], player_cd=True)
    m = _match_columns(c)
    if recent:
        # The microphone logged it first (as heard, or as another copy): it's this CD.
        db.run("""UPDATE plays SET kind = 'physical', detail = 'cd', station = NULL, instance_id = ?, release_id = ?,
                    record = ?, confidence = ?, alternatives = ?, note = ?, via = 'player' WHERE id = ?""",
               m["instance_id"], m["release_id"], m["record"], m["confidence"] or "metadata", m["alternatives"],
               m["note"], recent["id"])
        return
    end_open_metadata_play(room, ts)
    db.run("""INSERT INTO plays (room, kind, detail, started_at, title, artist, album, cover, song_key, instance_id, release_id,
                record, confidence, alternatives, note, via, created_at)
              VALUES (?, 'physical', 'cd', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'player', ?)""",
           room, ts, p["title"], p["artist"], p["album"], p.get("cover"), p["key"], m["instance_id"], m["release_id"],
           m["record"], m["confidence"] or "metadata", m["alternatives"], m["note"], now())


def end_open_metadata_play(room: str, ts: int) -> None:
    db.run("""UPDATE plays SET ended_at = ? WHERE room = ? AND (confidence = 'metadata' OR via = 'player')
              AND ended_at IS NULL AND started_at < ? AND started_at > ?""", ts, room, ts, ts - 60 * 60_000)


def apply_mic(event_id: str, room: str, ts: int, d: dict) -> None:
    title = str(d.get("title") or "").strip()
    artist = str(d.get("artist") or "").strip()
    album = str(d.get("album") or "").strip()
    cover = str(d.get("cover") or "") or None
    key = song_key(artist, title)
    if not key:
        return
    # The same song reported by another room (or again, or by the radio's text under another spelling): one play.
    existing = db.get("SELECT * FROM plays WHERE song_key = ? AND started_at BETWEEN ? AND ? "
                      "ORDER BY ABS(started_at - ?) LIMIT 1", key, ts - SAME_SONG_MS, ts + SAME_SONG_MS, ts) \
        or similar_play(ts, artist, title, 10 * 60_000)
    if existing and existing["artist"] != artist and cleaner_artist(existing["artist"], artist) == artist \
            and existing["confidence"] != "manual":
        # The microphone's (Shazam's) artist is the clean one.
        db.run("UPDATE plays SET artist = ?, song_key = ? WHERE id = ?", artist, key, existing["id"])
        existing.update(artist=artist, song_key=key)
    if existing:
        heard = list(dict.fromkeys(json.loads(existing["heard_by"]) + [room]))
        mic_ids = json.loads(existing["mic_ids"]) + [event_id]
        start = min(existing["started_at"], ts)
        if existing["confidence"] in ("metadata", "mic+metadata"):
            # The player already said it's streaming there: keep that, just note the confirmation.
            db.run("UPDATE plays SET heard_by = ?, mic_ids = ?, confidence = 'mic+metadata', cover = COALESCE(cover, ?) "
                   "WHERE id = ?", json.dumps(heard), json.dumps(mic_ids), cover, existing["id"])
            return
        if existing["confidence"] == "manual" or existing["via"] == "player":
            db.run("UPDATE plays SET heard_by = ?, mic_ids = ?, cover = COALESCE(cover, ?) WHERE id = ?",
                   json.dumps(heard), json.dumps(mic_ids), cover, existing["id"])
            return
        a = attribute(heard, start, existing["artist"], existing["title"], existing["album"] or album)
        save_play(existing["id"], a, {"start": start, "heard": heard, "mic_ids": mic_ids, "title": existing["title"],
                                      "artist": existing["artist"], "album": existing["album"] or album,
                                      "cover": existing["cover"] or cover, "key": key})
        return
    a = attribute([room], ts, artist, title, album)
    if a["kind"] == "streamed" and a["detail"] == "radio":
        # Radio metadata that disagrees with what the microphone heard: drop it.
        where = ("room = ? AND confidence = 'metadata' AND detail = 'radio' AND song_key <> ? AND started_at BETWEEN ? AND ?",
                 a["room"], key, ts - RADIO_CONFLICT_MS, ts + RADIO_CONFLICT_MS)
        records_sync.forget(db.all(f"SELECT * FROM plays WHERE {where[0]}", *where[1:]))
        db.run(f"DELETE FROM plays WHERE {where[0]}", *where[1:])
    save_play(None, a, {"start": ts, "heard": [room], "mic_ids": [event_id], "title": title, "artist": artist,
                        "album": album, "cover": cover, "key": key})


def save_play(play_id: int | None, a: dict, p: dict) -> None:
    m = _match_columns(a["collection"])
    confidence = m["confidence"] or ("metadata" if a["via"] == "player" else "mic")
    values = [a["room"], a["kind"], a["detail"], a["station"], p["start"], p["title"], p["artist"], p["album"],
              p["cover"], p["key"], m["instance_id"], m["release_id"], m["record"], confidence, json.dumps(p["heard"]),
              m["alternatives"], json.dumps(p["mic_ids"]), m["note"] if a["kind"] != "streamed" else None, a["via"]]
    if play_id is None:
        db.run("""INSERT INTO plays (room, kind, detail, station, started_at, title, artist, album, cover, song_key,
                    instance_id, release_id, record, confidence, heard_by, alternatives, mic_ids, note, via, created_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", *values, now())
    else:
        db.run("""UPDATE plays SET room = ?, kind = ?, detail = ?, station = ?, started_at = ?, title = ?, artist = ?,
                    album = ?, cover = ?, song_key = ?, instance_id = ?, release_id = ?, record = ?, confidence = ?,
                    heard_by = ?, alternatives = ?, mic_ids = ?, note = ?, via = ? WHERE id = ?""", *values, play_id)


def apply_mic_end(ts: int, d: dict) -> None:
    mic_id = re.sub(r'[%_"]', "", str(d.get("mic_id") or ""))
    if not mic_id:
        return
    db.run("UPDATE plays SET ended_at = MAX(COALESCE(ended_at, 0), ?) WHERE mic_ids LIKE ? AND started_at > ?",
           ts, f'%"{mic_id}"%', ts - 3 * 3600_000)


# ---------------------------------------------------------------- reading
def view(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        record = json.loads(r["record"]) if r.get("record") else None
        alts = [a for a in json.loads(r.get("alternatives") or "[]") if a.get("id") != r["instance_id"]] \
            if r["kind"] == "physical" else []
        out.append({
            "id": r["id"], "room": r["room"], "kind": r["kind"], "detail": r["detail"], "station": r["station"],
            "started_at": r["started_at"], "ended_at": r["ended_at"], "title": r["title"], "artist": r["artist"],
            "album": r["album"], "cover": (record or {}).get("cover") or r["cover"],
            "instance_id": r["instance_id"] if record else None,
            "record": {"id": r["instance_id"], **record} if record else None,
            "confidence": r["confidence"], "heard_by": json.loads(r["heard_by"]), "note": r["note"],
            "alternatives": alts, "origin": r.get("origin") or "hub",
        })
    return out


def list_plays(kind: str = "", room: str = "", q: str = "", limit: int = 60, offset: int = 0) -> dict:
    where, params = [], []
    if kind in ("physical", "streamed", "heard"):
        where.append("kind = ?")
        params.append(kind)
    if room:
        where.append("room = ?")
        params.append(room)
    if q:
        where.append("(title LIKE ? OR artist LIKE ? OR album LIKE ? OR station LIKE ?)")
        params += [f"%{q}%"] * 4
    w = f"WHERE {' AND '.join(where)}" if where else ""
    total = db.get(f"SELECT COUNT(*) AS n FROM plays {w}", *params)["n"]
    rows = db.all(f"SELECT * FROM plays {w} ORDER BY started_at DESC LIMIT ? OFFSET ?", *params, limit, offset)
    return {"total": total, "plays": view(rows)}


def _spin_key(r: dict) -> str | None:
    """Which copy a physical play was: your copy in JPlay Records, else the album (without JPlay Records)."""
    if r["instance_id"] is not None:
        return f"i{r['instance_id']}"
    if r["detail"] in ("vinyl", "cd") and (r["album"] or r["artist"]):
        return f"a{norm(r['artist'])}|{norm(r['album'])}" if r["album"] else None
    return None


def group_spins(rows: list[dict]) -> list[dict]:
    """Physical plays into spins: the same copy (or album) in the same room without a long gap."""
    spins: list[dict] = []
    open_: dict[str, dict] = {}
    for r in sorted(rows, key=lambda x: x["started_at"]):
        k = _spin_key(r)
        if not k:
            continue
        end = r["ended_at"] or r["started_at"] + TRACK_GUESS_MS
        s = open_.get(f"{r['room']}|{k}")
        if s and r["started_at"] - s["ended_at"] < SPIN_GAP_MS:
            s["ended_at"] = max(s["ended_at"], end)
            s["tracks"] += 1
        else:
            record = json.loads(r["record"]) if r.get("record") else None
            s = {"key": k, "instance_id": r["instance_id"], "room": r["room"], "medium": r["detail"],
                 "started_at": r["started_at"], "ended_at": end, "tracks": 1,
                 "record": {"id": r["instance_id"], **record} if record else
                 {"id": None, "artist": r["artist"], "title": r["album"], "year": None, "cover": r["cover"]}}
            spins.append(s)
            open_[f"{r['room']}|{k}"] = s
    return list(reversed(spins))


def recent_spins(limit: int = 30, room: str = "") -> list[dict]:
    rows = db.all("SELECT * FROM plays WHERE kind = 'physical' " + ("AND room = ? " if room else "")
                  + "ORDER BY started_at DESC LIMIT 1000", *([room] if room else []))
    return group_spins(rows)[:limit]


def now_playing() -> list[dict]:
    t = now()
    names = [r["name"] for r in rooms()]
    out = []
    for room in names:
        session = db.get("SELECT mode, station, started_at FROM room_sessions WHERE room = ? AND ended_at IS NULL "
                         "ORDER BY id DESC LIMIT 1", room)
        row = db.get("SELECT * FROM plays WHERE room = ? AND started_at > ? AND COALESCE(ended_at, started_at + ?) > ? "
                     "ORDER BY started_at DESC LIMIT 1", room, t - 3 * 3600_000, TRACK_GUESS_MS, t - 2 * 60_000)
        seen = db.get("SELECT MAX(last_seen_at) AS s FROM units WHERE room = ?", room)["s"]
        out.append({"room": room, "online": bool(seen) and t - seen < ROOM_STALE_MS, "session": session,
                    "play": view([row])[0] if row else None})
    return out


def stats(days: int) -> dict:
    t = now()
    since = t - days * 86_400_000 if days > 0 else 0
    count = lambda kind: db.get("SELECT COUNT(*) AS n FROM plays WHERE kind = ? AND started_at >= ?", kind, since)["n"]  # noqa: E731
    spins = group_spins(db.all("SELECT * FROM plays WHERE kind = 'physical' AND started_at >= ?", since))
    by_record: dict[str, dict] = {}
    for s in spins:
        e = by_record.setdefault(s["key"], {**s["record"], "spins": 0})
        e["spins"] += 1
    sessions = db.all("""SELECT room, mode, station,
                           SUM(MIN(COALESCE(ended_at, ?), ?) - MAX(started_at, ?)) / 3600000.0 AS hours
                         FROM room_sessions WHERE COALESCE(ended_at, ?) > ? GROUP BY room, mode, station
                         ORDER BY hours DESC""", t, t, since, t, since)
    return {
        "days": days,
        "physical": {"spins": len(spins), "tracks": count("physical"),
                     "vinyl_hours": sum((s["ended_at"] - s["started_at"]) / 3600_000 for s in spins if s["medium"] == "vinyl")},
        "streamed": {"tracks": count("streamed")},
        "heard": {"tracks": count("heard")},
        "radio": [{"room": s["room"], "station": s["station"] or "Radio", "hours": s["hours"]}
                  for s in sessions if s["mode"] == "radio"][:10],
        "listening_hours": sum(s["hours"] or 0 for s in sessions if s["mode"] != "cd"),
        "top_artists": db.all("SELECT MAX(artist) AS artist, COUNT(*) AS n FROM plays WHERE kind <> 'heard' AND artist <> '' "
                              "AND started_at >= ? GROUP BY lower(artist) ORDER BY n DESC LIMIT 10", since),
        "top_songs": db.all("SELECT MAX(artist) AS artist, MAX(title) AS title, COUNT(*) AS n FROM plays "
                            "WHERE started_at >= ? GROUP BY song_key ORDER BY n DESC LIMIT 10", since),
        "top_records": sorted(by_record.values(), key=lambda r: -r["spins"])[:12],
        "rooms": db.all("SELECT room, kind, COUNT(*) AS n FROM plays WHERE started_at >= ? GROUP BY room, kind", since),
    }


# ---------------------------------------------------------------- corrections from the Spins page
def delete_play(play_id: int) -> None:
    with db.tx():
        records_sync.forget(db.all("SELECT * FROM plays WHERE id = ?", play_id))
        db.run("DELETE FROM plays WHERE id = ?", play_id)


def mark_heard(play_id: int) -> None:
    db.run("UPDATE plays SET kind = 'heard', detail = '', instance_id = NULL, release_id = NULL, record = NULL, "
           "confidence = 'manual', note = 'Marked as not your record' WHERE id = ?", play_id)


def mark_physical(play_id: int, medium: str) -> bool:
    """"It was a record / a CD" (without saying which copy)."""
    if medium not in FORMAT_ORDER:
        return False
    db.run("UPDATE plays SET kind = 'physical', detail = ?, station = NULL, confidence = 'manual', "
           "note = 'Marked by you' WHERE id = ?", medium, play_id)
    return True


def set_play_record(play_id: int, instance_id: int) -> bool:
    """"It was this copy": one of the copies JPlay Records suggested for this play."""
    r = db.get("SELECT alternatives FROM plays WHERE id = ?", play_id)
    alt = next((a for a in json.loads((r or {}).get("alternatives") or "[]") if a.get("id") == instance_id), None)
    if not alt:
        return False
    record = {k: alt.get(k) for k in ("artist", "title", "year", "cover")}
    db.run("UPDATE plays SET kind = 'physical', detail = ?, instance_id = ?, record = ?, confidence = 'manual', "
           "note = 'Picked by you' WHERE id = ?", alt.get("medium") or "vinyl", instance_id, json.dumps(record), play_id)
    return True


def reset_play(play_id: int) -> None:
    """Undo a correction: let the matching decide again (streamed plays stay streamed)."""
    r = db.get("SELECT * FROM plays WHERE id = ? AND kind <> 'streamed'", play_id)
    if r:
        db.run("UPDATE plays SET confidence = 'mic' WHERE id = ?", play_id)
        r["confidence"] = "mic"
        rematch_rows([r], force=True)


def rematch(days: int = 90) -> int:
    """Match "heard" plays and physical plays without a specific copy again (JPlay Records got new records, or was
    away when they were played). Plays you corrected by hand are left alone."""
    if not records.enabled():
        return 0
    rows = db.all("""SELECT * FROM plays WHERE started_at >= ? AND confidence <> 'manual'
                       AND (kind = 'heard' OR (kind = 'physical' AND (instance_id IS NULL OR confidence IN ('likely', 'ambiguous'))))""",
                  now() - days * 86_400_000)
    return rematch_rows(rows)


def rematch_rows(rows: list[dict], force: bool = False) -> int:
    if not rows or not records.enabled():
        return 0
    info = room_info()
    songs = []
    for r in rows:
        player_cd = r["via"] == "player" and r["detail"] == "cd"
        songs.append({"artist": r["artist"], "title": r["title"], "album": r["album"], "player_cd": player_cd,
                      "formats": ["cd"] if player_cd else info.get(r["room"], {}).get("formats", [])})
    try:
        results = records.match_many(songs)
    except records.RecordsError as e:
        log.info("Matching again: %s", e)
        return 0
    changed = 0
    with db.tx():
        for r, s, res in zip(rows, songs, results):
            m = res.get("match")
            kind = "physical" if (m or s["player_cd"]) else "heard"
            detail = "cd" if s["player_cd"] else ((m or {}).get("medium") or "")
            cols = _match_columns({"match": m, "note": res.get("note") or "", "record": res.get("record")})
            if not force and kind == r["kind"] and cols["instance_id"] == r["instance_id"] and detail == r["detail"] \
                    and cols["note"] == r["note"]:
                continue
            db.run("""UPDATE plays SET kind = ?, detail = ?, instance_id = ?, release_id = ?, record = ?, confidence = ?,
                        alternatives = ?, note = ? WHERE id = ?""",
                   kind, detail, cols["instance_id"], cols["release_id"], cols["record"],
                   cols["confidence"] or ("metadata" if s["player_cd"] else "mic"), cols["alternatives"], cols["note"], r["id"])
            changed += 1
    return changed


def merge_duplicates(days: int = 30) -> int:
    """One play per song: merge plays of the same song within a few minutes of each other (the radio's text and
    the microphone, or two rooms). Keeps the play you corrected, else the microphone's, with the clean artist."""
    rows = db.all("SELECT * FROM plays WHERE started_at >= ? ORDER BY started_at", now() - days * 86_400_000)
    kept: list[dict] = []
    merged = 0

    def rank(r):
        return 3 if r["confidence"] == "manual" else 2 if r["kind"] == "physical" else 1 if "mic" in r["confidence"] else 0

    with db.tx():
        for r in rows:
            twin = next((k for k in kept if abs(k["started_at"] - r["started_at"]) <= 10 * 60_000 and same_song(k, r)), None)
            if not twin or (twin["confidence"] == "manual" and r["confidence"] == "manual"):
                kept.append(r)
                continue
            keep, drop = (r, twin) if rank(r) > rank(twin) else (twin, r)
            artist = keep["artist"] if keep["confidence"] == "manual" else cleaner_artist(keep["artist"], drop["artist"])
            heard = list(dict.fromkeys(json.loads(keep["heard_by"]) + json.loads(drop["heard_by"])))
            mic_ids = list(dict.fromkeys(json.loads(keep["mic_ids"]) + json.loads(drop["mic_ids"])))
            start = min(keep["started_at"], drop["started_at"])
            ended = None if keep["ended_at"] is None and drop["ended_at"] is None \
                else max(keep["ended_at"] or 0, drop["ended_at"] or 0)
            db.run("""UPDATE plays SET artist = ?, song_key = ?, started_at = ?, ended_at = ?, heard_by = ?, mic_ids = ?,
                        cover = COALESCE(cover, ?), station = COALESCE(station, ?) WHERE id = ?""",
                   artist, song_key(artist, keep["title"]), start, ended, json.dumps(heard), json.dumps(mic_ids),
                   drop["cover"], drop["station"], keep["id"])
            records_sync.forget([drop])
            db.run("DELETE FROM plays WHERE id = ?", drop["id"])
            keep.update(artist=artist, heard_by=json.dumps(heard), mic_ids=json.dumps(mic_ids), started_at=start)
            kept[kept.index(twin)] = keep
            merged += 1
    return merged
