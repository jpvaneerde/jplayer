"""Your music collection as the fingerprint service knows it: every file, its tags, and whether Panako has it.

Each file gets a number (its id) and a link DATA/links/<id>.<ext> -> the file, which is what Panako stores, so a
match tells the song by its number. Indexing (nightly, or when asked):
  - new files: read their tags, link, fingerprint (in batches)
  - changed files (new tags): read the tags again; the fingerprints stay (the music is the same)
  - moved or renamed files: recognised by size and tags, so they keep their number and aren't fingerprinted again
  - removed files: marked gone (matches to them are ignored; Panako can't delete, "rebuild" tidies up)
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .panako import Panako, PanakoError

log = logging.getLogger(__name__)
AUDIO = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".aif", ".aiff", ".wma", ".ape", ".wv"}
BATCH = 200            # files per Panako run
MAX_TRIES = 3          # a file Panako can't read is left after this many tries

SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  path TEXT NOT NULL UNIQUE,          -- relative to the music folder
  size INTEGER, mtime REAL,
  title TEXT, artist TEXT, album TEXT, albumartist TEXT, year TEXT, track TEXT, duration REAL,
  indexed INTEGER NOT NULL DEFAULT 0, -- 1: Panako has it
  tries INTEGER NOT NULL DEFAULT 0,
  gone INTEGER NOT NULL DEFAULT 0,    -- 1: the file isn't there any more
  added_at REAL, updated_at REAL
);
CREATE INDEX IF NOT EXISTS songs_pending ON songs (indexed, gone);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def read_tags(path: str) -> dict[str, Any]:
    """title, artist, album, albumartist, year, track, duration from the file (empty when it has none)."""
    out: dict[str, Any] = {"title": "", "artist": "", "album": "", "albumartist": "", "year": "", "track": "",
                           "duration": None}
    try:
        import mutagen

        f = mutagen.File(path, easy=True)
    except Exception as e:
        log.info("Tags of %s: %s", path, e)
        return out
    if f is None:
        return out

    def tag(*names: str) -> str:
        for n in names:
            v = (f.tags or {}).get(n) if f.tags is not None else None
            if v:
                return str(v[0] if isinstance(v, list) else v).strip()
        return ""
    out.update(title=tag("title"), artist=tag("artist"), album=tag("album"),
               albumartist=tag("albumartist", "album artist", "album_artist"),
               year=tag("date", "originaldate", "year")[:4], track=tag("tracknumber").split("/")[0])
    try:
        out["duration"] = round(float(f.info.length), 1)
    except Exception:
        pass
    if not out["title"]:
        out["title"] = Path(path).stem
    return out


class Library:
    def __init__(self, data_dir: str | Path, music_dir: str | Path, panako: Panako):
        self.data_dir = Path(data_dir)
        self.music_dir = Path(music_dir)
        self.links = self.data_dir / "links"
        self.panako = panako
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.links.mkdir(exist_ok=True)
        self._db = sqlite3.connect(self.data_dir / "library.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()          # one index run at a time
        self._dblock = threading.RLock()
        self.run: dict[str, Any] = {"running": False, "phase": "", "done": 0, "todo": 0, "started_at": None,
                                    "finished_at": self.meta("last_run_finished"), "error": "",
                                    "last": self._meta_json("last_run")}

    # ---------------------------------------------------------------- small helpers
    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self._dblock:
            return self._db.execute(sql, args).fetchall()

    def x(self, sql: str, *args) -> int:
        with self._dblock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur.lastrowid

    def meta(self, key: str) -> str | None:
        r = self.q("SELECT value FROM meta WHERE key = ?", key)
        return r[0]["value"] if r else None

    def set_meta(self, key: str, value: Any) -> None:
        self.x("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
               key, str(value))

    def _meta_json(self, key: str) -> Any:
        import json
        try:
            return json.loads(self.meta(key) or "null")
        except ValueError:
            return None

    def link(self, sid: int, rel: str) -> Path:
        dst = self.links / f"{sid}{Path(rel).suffix.lower()}"
        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.symlink_to(self.music_dir / rel)
        except OSError as e:
            log.warning("Link for %s: %s", rel, e)
        return dst

    def unlink(self, sid: int, rel: str) -> None:
        try:
            (self.links / f"{sid}{Path(rel).suffix.lower()}").unlink(missing_ok=True)
        except OSError:
            pass

    # ---------------------------------------------------------------- reading
    def song(self, sid: int) -> dict | None:
        r = self.q("SELECT * FROM songs WHERE id = ?", sid)
        return dict(r[0]) if r else None

    def counts(self) -> dict[str, int]:
        r = self.q("""SELECT COUNT(*) FILTER (WHERE gone = 0) AS files,
                             COUNT(*) FILTER (WHERE gone = 0 AND indexed = 1) AS indexed,
                             COUNT(*) FILTER (WHERE gone = 0 AND indexed = 0 AND tries < ?) AS waiting,
                             COUNT(*) FILTER (WHERE gone = 0 AND indexed = 0 AND tries >= ?) AS unreadable,
                             COUNT(*) FILTER (WHERE gone = 1) AS gone FROM songs""", MAX_TRIES, MAX_TRIES)[0]
        return {k: r[k] or 0 for k in r.keys()}

    # ---------------------------------------------------------------- indexing
    def scan(self) -> dict[str, tuple[int, float]]:
        found: dict[str, tuple[int, float]] = {}
        if not self.music_dir.is_dir():
            raise FileNotFoundError(f"The music folder {self.music_dir} isn't there (is the share mounted?)")
        for root, dirs, files in os.walk(self.music_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "@eaDir"]   # Synology's thumbnail folders
            for name in files:
                if name.startswith(".") or Path(name).suffix.lower() not in AUDIO:
                    continue
                full = os.path.join(root, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                found[os.path.relpath(full, self.music_dir)] = (st.st_size, st.st_mtime)
        return found

    def index(self, progress: Callable[[], None] | None = None) -> dict[str, int]:
        """Bring the index up to date with the music folder. Returns what changed."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("Indexing is already running")
        stats = {"new": 0, "changed": 0, "moved": 0, "gone": 0, "back": 0, "fingerprinted": 0, "failed": 0}
        self.run.update(running=True, phase="looking for files", done=0, todo=0, started_at=time.time(), error="")
        try:
            found = self.scan()
            known = {r["path"]: dict(r) for r in self.q("SELECT * FROM songs")}
            now = time.time()
            # Gone first, so a moved file can take over its number
            for rel, row in known.items():
                if rel not in found and not row["gone"]:
                    self.x("UPDATE songs SET gone = 1, updated_at = ? WHERE id = ?", now, row["id"])
                    self.unlink(row["id"], rel)
                    row["gone"] = 1
                    stats["gone"] += 1
            gone_by_size: dict[int, list[dict]] = {}
            for row in known.values():
                if row["gone"]:
                    gone_by_size.setdefault(row["size"], []).append(row)
            self.run["phase"] = "reading tags"
            for rel, (size, mtime) in found.items():
                row = known.get(rel)
                if row and not row["gone"]:
                    if row["size"] != size or abs((row["mtime"] or 0) - mtime) > 1:
                        t = read_tags(str(self.music_dir / rel))
                        self.x("""UPDATE songs SET size = ?, mtime = ?, title = ?, artist = ?, album = ?,
                                  albumartist = ?, year = ?, track = ?, duration = ?, updated_at = ? WHERE id = ?""",
                               size, mtime, t["title"], t["artist"], t["album"], t["albumartist"], t["year"],
                               t["track"], t["duration"], now, row["id"])
                        stats["changed"] += 1
                    continue
                if row and row["gone"]:                       # back where it was
                    self.x("UPDATE songs SET gone = 0, size = ?, mtime = ?, updated_at = ? WHERE id = ?",
                           size, mtime, now, row["id"])
                    self.link(row["id"], rel)
                    stats["back"] += 1
                    continue
                t = read_tags(str(self.music_dir / rel))
                moved = next((g for g in gone_by_size.get(size, [])
                              if g["gone"] and g["title"] == t["title"] and g["artist"] == t["artist"]), None)
                if moved:                                     # moved or renamed: keep its number and prints
                    moved["gone"] = 0
                    self.unlink(moved["id"], moved["path"])
                    self.x("""UPDATE songs SET path = ?, gone = 0, mtime = ?, album = ?, albumartist = ?, year = ?,
                              track = ?, updated_at = ? WHERE id = ?""",
                           rel, mtime, t["album"], t["albumartist"], t["year"], t["track"], now, moved["id"])
                    self.link(moved["id"], rel)
                    stats["moved"] += 1
                    continue
                sid = self.x("""INSERT INTO songs (path, size, mtime, title, artist, album, albumartist, year, track,
                                duration, added_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                             rel, size, mtime, t["title"], t["artist"], t["album"], t["albumartist"], t["year"],
                             t["track"], t["duration"], now, now)
                self.link(sid, rel)
                stats["new"] += 1
            # Fingerprint what Panako doesn't have yet
            pending = [dict(r) for r in self.q("SELECT id, path FROM songs WHERE gone = 0 AND indexed = 0 AND tries < ?"
                                               " ORDER BY id", MAX_TRIES)]
            self.run.update(phase="fingerprinting", todo=len(pending))
            for i in range(0, len(pending), BATCH):
                batch = pending[i:i + BATCH]
                paths = [str(self.links / f"{r['id']}{Path(r['path']).suffix.lower()}") for r in batch]
                try:
                    done = self.panako.store(paths)
                except PanakoError as e:
                    log.warning("Panako: %s", e)
                    self.run["error"] = str(e)
                    done = set()
                for r in batch:
                    if r["id"] in done:
                        self.x("UPDATE songs SET indexed = 1 WHERE id = ?", r["id"])
                        stats["fingerprinted"] += 1
                    else:
                        self.x("UPDATE songs SET tries = tries + 1 WHERE id = ?", r["id"])
                        stats["failed"] += 1
                self.run["done"] = min(len(pending), i + len(batch))
                if progress:
                    progress()
            return stats
        except Exception as e:
            self.run["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            import json
            fin = time.time()
            self.run.update(running=False, phase="", finished_at=fin, last={**stats, "seconds": round(fin - now)
                                                                             if "now" in locals() else 0})
            self.set_meta("last_run_finished", fin)
            self.set_meta("last_run", json.dumps(self.run["last"]))
            self._lock.release()

    def rebuild(self) -> None:
        """Start the index again: tidies away songs that are gone. Quick, the prints come from Panako's cache."""
        if self._lock.locked():
            raise RuntimeError("Indexing is running")
        self.panako.clear()
        self.x("UPDATE songs SET indexed = 0, tries = 0")
        self.x("DELETE FROM songs WHERE gone = 1")
