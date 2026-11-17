"""Lyrics from your music share: the hub looks through a Windows/Synology share (SMB) for .lrc files next to your
songs (made by LyricSync, or anything else) and gives them to every display, ahead of LRCLIB.

- Set up on the hub's Settings page: the share's address (\\\\synology\\music), a user and password (a read-only
  account is enough). Nothing is mounted on the Pi: the hub reads the share itself.
- Every few minutes (and on "Scan now") it walks the share. Only files that are new or changed since the last look
  are read; the rest is a quick look at each file's time. A changed file replaces the lyrics; when one song has
  several .lrc files (an album and a compilation), the newest one wins; a removed file sends the song back to LRCLIB.
- Which song a file belongs to: the tags of the audio file next to it (same name: "02 - Song.flac" for
  "02 - Song.lrc"), else the file's own [ar:]/[ti:] tags, else the folders (Artist/Album/NN - Title.lrc).

The password is kept in STATE/hub/lyrics_share.password (readable by JPlay alone) and never sent to a browser.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ..common import lrclib
from . import db, lyrics
from .db import now

log = logging.getLogger(__name__)

AUDIO_EXTS = (".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".aiff", ".aif", ".wma", ".ape", ".wv", ".alac")
SKIP_DIRS = {"@eadir", "#recycle", "#snapshot", ".snapshot", "@recycle", "$recycle.bin", "system volume information",
             "@tmp", ".appledouble", ".trash", ".trashes"}
MAX_LRC_BYTES = 512 * 1024             # bigger than any real lyrics file: not read
DEFAULT_EVERY = 15                     # minutes between looks
CONNECT_TIMEOUT = 10

_lock = threading.Lock()               # one scan at a time
status: dict[str, Any] = {"state": "idle", "message": "", "started_at": None, "finished_at": None,
                          "last": {}, "progress": 0}
_next_due = 0.0
_reset = False                         # the address changed during a scan: forget what it found when it ends


class ShareError(Exception):
    pass


# ---------------------------------------------------------------- settings
def _password_file() -> Path:
    from ..common import system
    p = system.state_dir("hub")
    p.mkdir(parents=True, exist_ok=True)
    return p / "lyrics_share.password"


def _password() -> str:
    try:
        return _password_file().read_text()
    except OSError:
        return ""


def config() -> dict:
    s = db.setting("lyrics_share", {}) or {}
    return {"address": s.get("address") or "", "username": s.get("username") or "",
            "every": int(s.get("every") or DEFAULT_EVERY), "on": bool(s.get("on", True)),
            "password_set": bool(_password())}


def parse_address(address: str) -> tuple[str, str, str]:
    """'\\\\synology\\music\\Albums', '//synology/music', 'smb://192.168.1.5/music' -> (server, share, folder)."""
    a = (address or "").strip()
    a = re.sub(r"^smb:", "", a, flags=re.I).replace("\\", "/").strip("/")
    parts = [p for p in a.split("/") if p]
    if len(parts) < 2:
        raise ShareError("Give the share as \\\\server\\share (optionally with a folder after it)")
    return parts[0], parts[1], "/".join(parts[2:])


def save(address: str, username: str, password: str | None, every: int, on: bool) -> dict:
    """Save the settings. A different address forgets what was found on the old one (and looks soon)."""
    global _next_due
    address = (address or "").strip()
    if address:
        parse_address(address)                       # raises on nonsense
    old = config()
    db.set_setting("lyrics_share", {"address": address, "username": (username or "").strip(),
                                    "every": max(5, min(int(every or DEFAULT_EVERY), 24 * 60)), "on": bool(on)})
    if password is not None and (password or not address):
        fd = os.open(_password_file(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(password)
    if _norm_addr(address) != _norm_addr(old["address"]):
        global _reset
        if _lock.acquire(timeout=2):
            try:
                _forget_all()
            finally:
                _lock.release()
        else:
            _reset = True                            # a scan of the old share is running: done when it ends
        status.update(state="idle", message="", last={}, finished_at=None)
    _next_due = 0.0                                  # look soon with the new settings
    return view()


def _forget_all() -> None:
    db.run("DELETE FROM lyric_files")
    lyrics.apply_files()                             # songs from the old share go back to LRCLIB


def _norm_addr(a: str) -> str:
    try:
        return "/".join(parse_address(a)).lower() if a else ""
    except ShareError:
        return a


def view() -> dict:
    c = config()
    counts = db.get("""SELECT COUNT(*) AS files, COALESCE(SUM(error IS NOT NULL), 0) AS problems,
                         COALESCE(SUM(synced LIKE '%]]]%'), 0) AS word_timed FROM lyric_files""")
    songs = db.get("SELECT COUNT(*) AS n FROM lyrics WHERE source = 'file'")["n"]
    problems = db.all("SELECT path, error FROM lyric_files WHERE error IS NOT NULL ORDER BY scanned_at DESC LIMIT 8")
    return {**c, "status": dict(status), "files": counts["files"], "songs": songs, "word_timed": counts["word_timed"],
            "problems": counts["problems"], "problem_files": problems,
            "next_scan_in": max(0, int(_next_due - time.time())) if c["address"] and c["on"] else None}


# ---------------------------------------------------------------- SMB
def _smb():
    import smbclient                                 # smbprotocol; imported late so the hub runs without it
    return smbclient


class _Share:
    """A connection to the share for one scan or test."""

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or config()
        if not cfg["address"]:
            raise ShareError("No share set up")
        self.server, self.share, self.folder = parse_address(cfg["address"])
        self.root = f"\\\\{self.server}\\{self.share}" + (("\\" + self.folder.replace("/", "\\")) if self.folder else "")
        self.smb = _smb()
        try:
            pw = cfg["password"] if cfg.get("password") is not None else _password()
            self.smb.register_session(self.server, username=cfg["username"] or None, password=pw or None,
                                      connection_timeout=CONNECT_TIMEOUT)
        except Exception as e:
            raise ShareError(_explain(e)) from e

    def path(self, rel: str) -> str:
        return self.root + ("\\" + rel.replace("/", "\\") if rel else "")

    def scandir(self, rel: str):
        return self.smb.scandir(self.path(rel))

    def read(self, rel: str, limit: int = MAX_LRC_BYTES) -> bytes:
        with self.smb.open_file(self.path(rel), mode="rb") as f:
            return f.read(limit + 1)

    def open(self, rel: str):
        return self.smb.open_file(self.path(rel), mode="rb")

    def close(self) -> None:
        try:
            self.smb.delete_session(self.server)
        except Exception:
            pass


_NOT_THERE = "That folder isn't on the share"


def _explain(e: Exception) -> str:
    """An SMB error in words."""
    t = f"{type(e).__name__}: {e}"
    low = t.lower()
    if "logon_failure" in low or "logonfailure" in low or "access_denied" in low and "logon" in low:
        return "The share didn't accept the user name or password"
    if "bad_network_name" in low:
        return "The server is there, but it has no share by that name"
    if "access_denied" in low or "accessdenied" in low:
        return "No access: give this user read rights on the share"
    if "object_name_not_found" in low or "object_path_not_found" in low or "no such file" in low \
            or "0xc0000225" in low:
        return _NOT_THERE
    if "timed out" in low or "timeout" in low or "getaddrinfo" in low or "name or service" in low \
            or "connection refused" in low or "unreachable" in low:
        return "Can't reach the server (check the name or address, and that the NAS is on)"
    return t[:240]


def test(cfg: dict | None = None) -> dict:
    """Connect and look at the top folder: {"ok", "message"}."""
    try:
        s = _Share(cfg)
    except ShareError as e:
        return {"ok": False, "message": str(e)}
    except ImportError:
        return {"ok": False, "message": "smbprotocol isn't installed on this Pi (run the installer once more)"}
    try:
        dirs = lrcs = 0
        for e in s.scandir(""):
            if e.is_dir():
                dirs += 1
            elif e.name.lower().endswith(".lrc"):
                lrcs += 1
        plural = lambda n, w: f"{n} {w}" + ("" if n == 1 else "s")   # noqa: E731
        return {"ok": True, "message": f"Connected: {plural(dirs, 'folder')}" + (f" and {plural(lrcs, '.lrc file')}" if lrcs else "")
                + " at the top. The scan looks through all of them."}
    except Exception as e:
        msg = _explain(e)
        if msg == _NOT_THERE:                        # the share itself, or only the folder in it?
            try:
                next(iter(s.smb.scandir(f"\\\\{s.server}\\{s.share}")), None)
                msg = f"The share is there, but it has no folder {s.folder!r}" if s.folder else msg
            except Exception:
                msg = f"{s.server} has no share called {s.share!r}"
        return {"ok": False, "message": msg}
    finally:
        s.close()


# ---------------------------------------------------------------- which song a file is
def _tags(share: _Share, rel: str) -> dict:
    """artist / title / album / duration from an audio file's tags (read from the share)."""
    import mutagen
    with share.open(rel) as f:
        m = mutagen.File(f, easy=True)
    if m is None:
        return {}
    out: dict[str, Any] = {}
    tags = m.tags or {}
    for k in ("artist", "albumartist", "title", "album"):
        try:
            v = tags.get(k)
        except Exception:
            v = None
        if v:
            out[k] = str(v[0] if isinstance(v, list) else v).strip()
    if getattr(m, "info", None) and getattr(m.info, "length", None):
        out["duration"] = round(float(m.info.length), 1)
    return out


def _length(v: str) -> float | None:
    m = re.match(r"^\s*(\d+):(\d+(?:\.\d+)?)\s*$", v or "")
    return int(m.group(1)) * 60 + float(m.group(2)) if m else None


def _from_path(rel: str) -> dict:
    """Artist/Album/02 - Title.lrc -> artist, album, title (Plex's layout)."""
    p = PurePosixPath(rel)
    title = re.sub(r"^\s*(\d+[-. ]*)?\d+\s*[-.]\s*", "", p.stem).strip() or p.stem
    parents = p.parent.parts
    return {"title": title, "album": parents[-1] if parents else "", "artist": parents[-2] if len(parents) >= 2 else ""}


def identify(share: _Share, rel: str, audio: str | None, text: str) -> dict:
    info: dict[str, Any] = {}
    if audio:
        try:
            info = _tags(share, audio)
        except Exception as e:
            log.debug("Lyrics share: tags of %s: %s", audio, e)
    hdr = lrclib.lrc_tags(text)
    guess = _from_path(rel)
    artist = info.get("artist") or info.get("albumartist") or hdr.get("ar") or guess["artist"]
    title = info.get("title") or hdr.get("ti") or guess["title"]
    album = info.get("album") or hdr.get("al") or guess["album"]
    duration = info.get("duration") or _length(hdr.get("length", ""))
    return {"artist": artist, "title": title, "album": album, "duration": duration}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", errors="replace")


def read_file(share: _Share, rel: str, audio: str | None) -> dict:
    """One .lrc -> the lyric_files row (without path/mtime/size)."""
    raw = share.read(rel)
    if len(raw) > MAX_LRC_BYTES:
        return {"error": "Too big for a lyrics file"}
    text = _decode(raw)
    synced = lrclib.parse_lrc(text)
    plain = [e[1] for e in synced] if synced else \
        [ln.rstrip() for ln in text.strip().splitlines() if not re.match(r"^\s*\[[a-z]{2,8}:.*\]\s*$", ln, re.I)]
    if not synced and not any(plain):
        return {"error": "No lyrics in it"}
    who = identify(share, rel, audio, text)
    key = lrclib.cache_key(who["artist"], who["title"]) if who["artist"] and who["title"] else None
    if not key or not all(key.split("|")):
        return {**who, "error": "Couldn't tell which song it is (no tags, and the file name doesn't say)"}
    return {**who, "key": key, "synced": json.dumps(synced) if synced else None,
            "plain": json.dumps(plain) if any(plain) else None, "error": None}


# ---------------------------------------------------------------- the scan
def _walk(share: _Share, rel: str, found: dict, errors: list) -> None:
    """Every .lrc under rel -> found[path] = (mtime_ms, size, audio sibling or None)."""
    try:
        entries = list(share.scandir(rel))
    except Exception as e:
        errors.append(f"{rel or '(top)'}: {_explain(e)}")
        return
    names = {}
    for e in entries:
        if not e.is_dir():
            names[e.name.lower()] = e.name
    for e in entries:
        name = e.name
        if name.startswith(".") or name.lower() in SKIP_DIRS:
            continue
        sub = f"{rel}/{name}" if rel else name
        if e.is_dir():
            _walk(share, sub, found, errors)
        elif name.lower().endswith(".lrc"):
            st = e.stat()
            stem = name[:-4].lower()
            audio = next((names[stem + x] for x in AUDIO_EXTS if stem + x in names), None)
            found[sub] = (int(st.st_mtime * 1000), int(st.st_size),
                          (f"{rel}/{audio}" if rel else audio) if audio else None)
            status["progress"] = len(found)


def scan(full: bool = False) -> dict | None:
    """Look through the share now. full: read every file again (not only new and changed ones).
    None when a scan is already running."""
    global _next_due
    if not _lock.acquire(blocking=False):
        return None
    try:
        cfg = config()
        _next_due = time.time() + cfg["every"] * 60
        if not cfg["address"]:
            return {}
        status.update(state="scanning", message="Connecting", started_at=now(), progress=0)
        try:
            share = _Share(cfg)
        except ImportError:
            status.update(state="failed", message="smbprotocol isn't installed on this Pi (run the installer once more)",
                          finished_at=now())
            return None
        except ShareError as e:
            status.update(state="failed", message=str(e), finished_at=now())
            return None
        counts = {"files": 0, "new": 0, "changed": 0, "gone": 0, "problems": 0}
        try:
            status["message"] = "Looking for .lrc files"
            found: dict[str, tuple] = {}
            errors: list[str] = []
            _walk(share, "", found, errors)
            known = {r["path"]: r for r in db.all("SELECT path, mtime, size FROM lyric_files")}
            todo = [p for p, (mt, sz, _) in found.items()
                    if full or p not in known or known[p]["mtime"] != mt or known[p]["size"] != sz]
            counts["files"] = len(found)
            for i, path in enumerate(todo, 1):
                status["message"] = f"Reading {i} of {len(todo)}"
                mtime, size, audio = found[path]
                try:
                    row = read_file(share, path, audio)
                except Exception as e:
                    row = {"error": _explain(e)}
                counts["changed" if path in known else "new"] += 1
                counts["problems"] += bool(row.get("error"))
                db.run("""INSERT INTO lyric_files (path, mtime, size, key, artist, title, album, duration, synced, plain,
                            error, scanned_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                          ON CONFLICT(path) DO UPDATE SET mtime = excluded.mtime, size = excluded.size, key = excluded.key,
                            artist = excluded.artist, title = excluded.title, album = excluded.album,
                            duration = excluded.duration, synced = excluded.synced, plain = excluded.plain,
                            error = excluded.error, scanned_at = excluded.scanned_at""",
                       path, mtime, size, row.get("key"), row.get("artist") or "", row.get("title") or "",
                       row.get("album") or "", row.get("duration"), row.get("synced"), row.get("plain"),
                       row.get("error"), now())
            # Files that are gone: only when every folder could be read (else a hiccup would wipe lyrics)
            if not errors:
                gone = [p for p in known if p not in found]
                with db.tx():
                    for p in gone:
                        db.run("DELETE FROM lyric_files WHERE path = ?", p)
                counts["gone"] = len(gone)
            applied = lyrics.apply_files()
            counts.update(applied)
            msg = "" if not errors else f"{len(errors)} folders couldn't be read (removed files are kept until they can): " \
                + "; ".join(errors[:3])
            status.update(state="idle" if not errors else "partly", message=msg, finished_at=now(), last=counts)
            if counts["new"] or counts["changed"] or counts["gone"]:
                log.info("Lyrics share: %s", counts)
            return counts
        except Exception as e:
            log.exception("Lyrics share scan")
            status.update(state="failed", message=_explain(e), finished_at=now(), last=counts)
            return None
        finally:
            share.close()
    finally:
        global _reset
        if _reset:
            _reset = False
            _forget_all()
            status.update(state="idle", message="", last={})
            _next_due = 0.0
        _lock.release()


def running() -> bool:
    return _lock.locked()


def due() -> bool:
    c = config()
    return bool(c["address"] and c["on"] and time.time() >= _next_due and not running())


def scan_in_background(full: bool = False) -> bool:
    if running():
        return False
    threading.Thread(target=scan, args=(full,), name="lyrics-share", daemon=True).start()
    return True
