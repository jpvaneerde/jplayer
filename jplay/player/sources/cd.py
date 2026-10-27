"""Audio CD: play while reading, keep the disc in RAM.

* The drive is polled with Linux CD-ROM ioctls (no udev dependency).
* Inserting a disc starts reading it into a tmpfs (/dev/shm) straight away, in play order,
  but never starts playback or takes over from the source that is playing.
* Playback starts after a second or two: mpv streams each track from JPlay's own HTTP
  endpoint (/api/v1/cd/stream/...), which hands out what cd-paranoia has read so far and
  waits when mpv gets ahead of the drive. Once the whole disc is in RAM the drive rests.
* Disc metadata comes from MusicBrainz (by disc ID) and is cached on disk.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import struct
from pathlib import Path
from typing import Any, AsyncIterator

from ..core.models import Capabilities, PlayState
from .base import SourceError
from .engine_source import EngineSource

log = logging.getLogger(__name__)

BYTES_PER_SECTOR = 2352  # 1/75 s of 16-bit stereo 44.1 kHz audio
WAV_HEADER_BYTES = 44
# How long the player may wait for a slow drive before it gives up on a stream.
STREAM_TIMEOUT = 120

# cd-paranoia error-correction levels, from careful to fast.
PARANOIA_FLAGS = {"full": [], "light": ["-Y"], "off": ["-Z"]}

# linux/cdrom.h
CDROM_DRIVE_STATUS = 0x5326
CDROM_DISC_STATUS = 0x5327
CDSL_CURRENT = 0x7FFFFFFF
CDS_DISC_OK = 4
CDS_AUDIO = 100
CDS_MIXED = 105

USER_AGENT = "JPlay/0.1 ( https://github.com/jpvaneerde/jplayer )"

_TOC_LINE = re.compile(
    r"^\s*(\d+)\.\s+(\d+)\s+\[[^\]]*\]\s+(\d+)\s+\[[^\]]*\]\s+(\w+)\s+(\w+)\s+(\d+)", re.M
)


# ---------------------------------------------------------------- pure helpers (unit tested)
def parse_paranoia_toc(text: str) -> list[dict[str, Any]]:
    """Parse `cd-paranoia -Q` output into [{number, start, length, preemphasis}] (sectors)."""
    tracks = []
    for m in _TOC_LINE.finditer(text):
        number, length, start, _copy, pre, _ch = m.groups()
        tracks.append(
            {"number": int(number), "start": int(start), "length": int(length), "preemphasis": pre == "yes"}
        )
    return tracks


def musicbrainz_disc_id(first: int, last: int, leadout: int, offsets: list[int]) -> str:
    """MusicBrainz disc ID. Offsets and lead-out are absolute sectors (LBA + 150)."""
    padded = list(offsets) + [0] * (99 - len(offsets))
    text = f"{first:02X}{last:02X}{leadout:08X}" + "".join(f"{o:08X}" for o in padded)
    digest = base64.b64encode(hashlib.sha1(text.encode("ascii")).digest()).decode("ascii")
    return digest.replace("+", ".").replace("/", "_").replace("=", "-")


def disc_id_from_toc(tracks: list[dict[str, Any]]) -> str:
    first, last = tracks[0]["number"], tracks[-1]["number"]
    leadout = tracks[-1]["start"] + tracks[-1]["length"] + 150
    return musicbrainz_disc_id(first, last, leadout, [t["start"] + 150 for t in tracks])


def parse_musicbrainz(data: dict[str, Any], disc_id: str) -> dict[str, Any] | None:
    """Pick the release/medium that contains this exact disc and return album + track names."""

    def credit(ac: list[dict[str, Any]] | None) -> str:
        return "".join(f"{c.get('name', '')}{c.get('joinphrase', '')}" for c in (ac or []))

    for release in data.get("releases", []):
        for medium in release.get("media", []):
            if not any(d.get("id") == disc_id for d in medium.get("discs", [])):
                continue
            album_artist = credit(release.get("artist-credit"))
            tracks = {}
            for t in medium.get("tracks", []):
                try:
                    pos = int(t.get("position") or t.get("number"))
                except (TypeError, ValueError):
                    continue
                tracks[pos] = {
                    "title": t.get("title") or (t.get("recording") or {}).get("title", ""),
                    "artist": credit(t.get("artist-credit")) or album_artist,
                }
            has_front = (release.get("cover-art-archive") or {}).get("front")
            return {
                "album": release.get("title", ""),
                "artist": album_artist,
                "year": (release.get("date") or "")[:4],
                "release_id": release.get("id"),
                "art_url": f"https://coverartarchive.org/release/{release['id']}/front-500" if has_front else "",
                "medium": medium.get("position"),
                "media_count": len(release.get("media", [])),
                "tracks": tracks,
            }
    return None


def _drive_status(device: str) -> str:
    """'audio', 'data', 'empty' or 'error'. Linux only."""
    import fcntl

    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return "error"
    try:
        if fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT) != CDS_DISC_OK:
            return "empty"
        disc = fcntl.ioctl(fd, CDROM_DISC_STATUS, 0)
        return "audio" if disc in (CDS_AUDIO, CDS_MIXED) else "data"
    except OSError:
        return "empty"
    finally:
        os.close(fd)


def wav_header(data_bytes: int) -> bytes:
    """44-byte header for 16-bit stereo 44.1 kHz PCM of the given length."""
    return (
        b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, 44100, 44100 * 4, 4, 16)
        + b"data" + struct.pack("<I", data_bytes)
    )


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """'bytes=100-' / 'bytes=100-199' -> (start, end) inclusive; None if absent or unusable."""
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", header or "")
    if not m or (not m.group(1) and not m.group(2)):
        return None
    if m.group(1):
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else size - 1
    else:  # suffix range: the last N bytes
        start, end = max(0, size - int(m.group(2))), size - 1
    if start >= size:
        return None
    return start, min(end, size - 1)


class TrackBuffer:
    """One track in RAM: a WAV file that cd-paranoia is (or was) filling.

    Readers wait on it for more bytes; `generation` changes when the buffer is thrown away
    (re-read, eject), which ends every stream still reading the old data.
    """

    def __init__(self, number: int, path: Path, sectors: int):
        self.number = number
        self.path = path
        self.size = WAV_HEADER_BYTES + sectors * BYTES_PER_SECTOR
        self.written = 0
        self.done = False
        self.failed = False
        self.generation = 0
        self._changed = asyncio.Condition()

    @property
    def percent(self) -> int:
        return 100 if self.done else int(100 * self.written / self.size)

    async def notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def reset(self) -> None:
        self.generation += 1
        self.written = 0
        self.done = self.failed = False
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        await self.notify()

    async def wait_for(self, offset: int, generation: int, timeout: float) -> bool:
        """Wait until byte `offset` is in. False if the buffer was dropped or the wait timed out."""
        async def ready() -> bool:
            async with self._changed:
                await self._changed.wait_for(
                    lambda: self.written > offset or self.done or self.failed or self.generation != generation
                )
            return self.written > offset and self.generation == generation

        try:
            return await asyncio.wait_for(ready(), timeout)
        except asyncio.TimeoutError:
            return False

    async def stream(self, start: int, end: int, timeout: float = STREAM_TIMEOUT) -> AsyncIterator[bytes]:
        """Bytes start..end (inclusive), as soon as the drive has read them."""
        generation = self.generation
        pos = start
        while pos <= end:
            if not await self.wait_for(pos, generation, timeout):
                return
            with open(self.path, "rb") as fh:
                fh.seek(pos)
                chunk = fh.read(min(self.written, end + 1) - pos)
            if not chunk:
                return
            pos += len(chunk)
            yield chunk


# ---------------------------------------------------------------- the source
class CdSource(EngineSource):
    id = "cd"
    name = "CD"

    def __init__(self, ctx):
        super().__init__(ctx)
        self.device = self.cfg.get("device", "/dev/sr0")
        self.paranoia = self.cfg.get("paranoia_binary", "cd-paranoia")
        self.cache_dir = Path(self.cfg.get("cache_dir", "/dev/shm/jplay-cd"))
        self.meta_dir = ctx.state_dir / "cd-metadata"
        self.status = "empty"  # empty | reading-toc | ready | data | error
        self.disc: dict[str, Any] | None = None
        self.buffers: dict[int, TrackBuffer] = {}
        self.reading: int | None = None  # track cd-paranoia is reading right now
        self.base: int | None = None  # track number at mpv playlist index 0
        self.current: int | None = None
        self.token = ""  # changes with every disc, so stale stream URLs fail
        self.drive_status = _drive_status  # replaceable in tests
        self._poll_task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------ startup / polling
    async def start(self) -> None:
        ok, reason = self.engine_ready()
        if not ok:
            self.set_available(False, reason)
            return
        if shutil.which(self.paranoia) is None:
            self.set_available(False, f"'{self.paranoia}' is not installed")
            return
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.set_available(False, "CD playback needs Linux")
            return
        if not Path(self.device).exists():
            # The drive may be plugged in later; keep polling.
            self.set_available(True, f"No drive at {self.device} yet")
        else:
            self.set_available(True)
        self._poll_task = asyncio.create_task(self._poll(), name="cd-poll")

    async def shutdown(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
        await self._stop_reading()
        self._clear_cache()

    async def _poll(self) -> None:
        last = None
        while True:
            try:
                state = await asyncio.to_thread(self.drive_status, self.device)
            except Exception:
                state = "error"
            if state != last:
                last = state
                try:
                    await self._drive_changed(state)
                except Exception:
                    log.exception("Handling the CD drive failed")
            await asyncio.sleep(1.5)

    async def _drive_changed(self, state: str) -> None:
        log.info("CD drive: %s", state)
        if state == "error":
            self.set_available(True, f"No drive at {self.device}")
        else:
            self.set_available(True)
        if state == "audio":
            await self._load_disc()
        else:
            await self._disc_gone(data=state == "data")

    # ------------------------------------------------------------ disc handling
    async def _load_disc(self) -> None:
        self.status = "reading-toc"
        self.ctx.manager.notify(True)
        proc = await asyncio.create_subprocess_exec(
            self.paranoia, "-d", self.device, "-Q",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        toc = parse_paranoia_toc(out.decode(errors="replace"))
        if not toc:
            log.warning("Could not read the CD's table of contents")
            self.status = "error"
            self.ctx.manager.notify(True)
            return
        disc_id = disc_id_from_toc(toc)
        self.disc = {
            "disc_id": disc_id,
            "album": "Audio CD",
            "artist": "",
            "year": "",
            "art_url": "",
            "tracks": [
                {
                    "number": t["number"],
                    "title": f"Track {t['number']}",
                    "artist": "",
                    "duration": round(t["length"] / 75, 1),
                    "preemphasis": t["preemphasis"],
                }
                for t in toc
            ],
        }
        self.token = os.urandom(6).hex()
        self._clear_cache()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.buffers = {
            t["number"]: TrackBuffer(t["number"], self.cache_dir / f"track{t['number']:02d}.wav", t["length"])
            for t in toc
        }
        self.base = self.current = None
        self.status = "ready"
        # Start reading straight away. This doesn't touch playback: whatever plays keeps playing.
        self._read_task = asyncio.create_task(self._read_loop(), name="cd-read")
        self.ctx.manager.notify(True)
        asyncio.create_task(self._lookup_metadata(disc_id))
        if self.cfg.get("autoplay", False):
            asyncio.create_task(self._autoplay(toc[0]["number"]))

    async def _autoplay(self, number: int) -> None:
        try:
            await self.play_track(number)
        except SourceError as exc:
            log.info("Autoplay skipped: %s", exc)

    async def _disc_gone(self, data: bool = False) -> None:
        if self.disc is not None and self.owns_engine:
            await self.engine.release(self)
        await self._stop_reading()
        for buf in self.buffers.values():
            await buf.reset()  # ends any stream still reading this disc
        self.buffers = {}
        self._clear_cache()
        self.disc = None
        self.token = ""
        self.base = self.current = None
        self.status = "data" if data else "empty"
        self.reset_now_playing()

    async def eject(self) -> None:
        await self._disc_gone()
        proc = await asyncio.create_subprocess_exec(
            "eject", self.device, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            raise SourceError(f"Eject failed: {err.decode(errors='replace').strip()}", 500)

    def _clear_cache(self) -> None:
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    # ------------------------------------------------------------ metadata
    async def _lookup_metadata(self, disc_id: str) -> None:
        if not self.cfg.get("musicbrainz", True):
            return
        cache = self.meta_dir / f"{disc_id}.json"
        meta = None
        if cache.exists():
            with contextlib.suppress(Exception):
                meta = json.loads(cache.read_text("utf-8"))
        if meta is None:
            try:
                resp = await self.ctx.http.get(
                    f"https://musicbrainz.org/ws/2/discid/{disc_id}",
                    params={"inc": "recordings artist-credits", "fmt": "json", "cdstubs": "no"},
                    headers={"User-Agent": USER_AGENT},
                    timeout=20,
                )
                if resp.status_code == 404:
                    log.info("Disc %s is not in MusicBrainz", disc_id)
                    return
                resp.raise_for_status()
                meta = parse_musicbrainz(resp.json(), disc_id)
            except Exception as exc:
                log.warning("MusicBrainz lookup failed: %s", exc)
                return
            if meta is None:
                return
            self.meta_dir.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(meta, ensure_ascii=False), "utf-8")
        if not self.disc or self.disc["disc_id"] != disc_id:
            return  # disc changed meanwhile
        self.disc.update({k: meta.get(k, "") for k in ("album", "artist", "year", "art_url")})
        for t in self.disc["tracks"]:
            info = meta.get("tracks", {}).get(str(t["number"])) or meta.get("tracks", {}).get(t["number"])
            if info:
                t["title"], t["artist"] = info["title"], info["artist"]
        if self.current is not None:
            self._show(self.current)
        self.ctx.manager.notify(True)

    # ------------------------------------------------------------ reading the disc
    def _numbers(self) -> list[int]:
        return [t["number"] for t in self.disc["tracks"]] if self.disc else []

    def _next_to_read(self) -> int | None:
        numbers = self._numbers()
        if not numbers:
            return None
        # Start from the track that is playing (or about to), then wrap around.
        start = next((n for n in (self.current, self.base) if n in numbers), numbers[0])
        i = numbers.index(start)
        for n in numbers[i:] + numbers[:i]:
            buf = self.buffers.get(n)
            if buf and not buf.done and not buf.failed:
                return n
        return None

    def _paranoia_args(self, number: int) -> list[str]:
        args = [self.paranoia, "-q", "-d", self.device]
        speed = int(self.cfg.get("read_speed", 0) or 0)
        if speed > 0:
            args += ["-S", str(speed)]
        args += PARANOIA_FLAGS.get(self.cfg.get("paranoia", "light"), PARANOIA_FLAGS["light"])
        # -r: raw samples (no WAV header) in host byte order, which is little-endian on a Pi.
        return args + ["-r", str(number), "-"]

    async def _read_loop(self) -> None:
        while True:
            number = self._next_to_read()
            if number is None:
                self.reading = None
                self.ctx.manager.notify(True)
                if any(b.failed for b in self.buffers.values()):
                    log.warning("Disc read with errors")
                else:
                    log.info("Whole disc is in memory; the drive can rest")
                return
            await self._read_track(self.buffers[number])

    async def _read_track(self, buf: TrackBuffer) -> None:
        self.reading = buf.number
        if buf.written or buf.done or buf.failed:
            # Leftovers from an interrupted read. (A fresh buffer is left alone: the player
            # may already be waiting on it, e.g. for the next track.)
            await buf.reset()
        self.ctx.manager.notify(True)
        proc = await asyncio.create_subprocess_exec(
            *self._paranoia_args(buf.number),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None and proc.stderr is not None
        err_task = asyncio.create_task(proc.stderr.read())
        try:
            with open(buf.path, "wb") as fh:
                fh.write(wav_header(buf.size - WAV_HEADER_BYTES))
                fh.flush()
                buf.written = WAV_HEADER_BYTES
                await buf.notify()
                while True:
                    chunk = await proc.stdout.read(256 * 1024)
                    if not chunk:
                        break
                    room = buf.size - buf.written
                    fh.write(chunk[:room])
                    fh.flush()
                    buf.written += min(len(chunk), room)
                    await buf.notify()
                    self.ctx.manager.notify(False)  # progress for the UI, rate-limited
            code = await proc.wait()
            err = (await err_task).decode(errors="replace")
            if code != 0:
                log.error("Reading track %s failed (%s): %s", buf.number, code, err[-300:])
                buf.failed = True
                self.update(extra={**self.np.extra, "error": f"Track {buf.number} could not be read"})
            elif buf.written < buf.size:
                # The drive delivered a little less than the TOC promised; pad with silence
                # so the length the player was told stays true.
                with open(buf.path, "ab") as fh:
                    fh.write(b"\0" * (buf.size - buf.written))
                buf.written = buf.size
            if not buf.failed:
                buf.done = True
            await buf.notify()
            self.ctx.manager.notify(True)
        except asyncio.CancelledError:
            # Interrupted (jump to another track, eject): this partial read is thrown away.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
            err_task.cancel()
            await buf.reset()
            raise
        finally:
            self.reading = None if self.reading == buf.number else self.reading

    async def _stop_reading(self) -> None:
        task, self._read_task = self._read_task, None
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.reading = None

    async def _reprioritise(self, number: int) -> None:
        """Make sure track `number` is read next (or now)."""
        buf = self.buffers.get(number)
        if buf is None or buf.done or self.reading == number:
            return
        if self._read_task and not self._read_task.done() and self.reading is not None:
            await self._stop_reading()
        if not self._read_task or self._read_task.done():
            self._read_task = asyncio.create_task(self._read_loop(), name="cd-read")

    # ------------------------------------------------------------ streaming to mpv
    def buffer_for(self, token: str, number: int) -> TrackBuffer | None:
        if not token or token != self.token:
            return None
        return self.buffers.get(number)

    def _stream_url(self, number: int) -> str:
        port = self.ctx.config["server"]["port"]
        return f"http://127.0.0.1:{port}/api/v1/cd/stream/{self.token}/{number}.wav"

    def engine_properties(self) -> dict[str, Any]:
        # A scratched disc can make the drive pause; let mpv wait for it.
        return {"network-timeout": STREAM_TIMEOUT}

    # ------------------------------------------------------------ playback
    def _track(self, number: int) -> dict[str, Any]:
        for t in self.disc["tracks"] if self.disc else []:
            if t["number"] == number:
                return t
        raise SourceError(f"No track {number} on this disc", 404)

    def _show(self, number: int, state: PlayState | None = None) -> None:
        if not self.disc:
            return
        t = self._track(number)
        self.current = number
        extra = {"disc_id": self.disc["disc_id"]}
        if t.get("preemphasis"):
            extra["preemphasis"] = True
        self.reset_now_playing(
            state=state or self.np.state,
            title=t["title"],
            artist=t["artist"] or self.disc["artist"],
            album=self.disc["album"],
            art_url=self.disc["art_url"],
            duration=t["duration"],
            position=0.0,
            track_number=number,
            track_count=len(self.disc["tracks"]),
            extra=extra,
        )

    async def play_track(self, number: int) -> None:
        if not self.disc:
            raise SourceError("No audio CD in the drive", 409)
        self._track(number)
        if self.buffers[number].failed:
            raise SourceError(f"Track {number} could not be read from the disc", 500)
        await self.claim()
        self.base = number
        self._show(number, PlayState.LOADING)
        await self._reprioritise(number)
        # The whole rest of the disc goes into mpv's playlist at once: gapless, and each
        # stream simply waits for the drive if mpv gets there first.
        numbers = self._numbers()
        urls = [self._stream_url(n) for n in numbers[numbers.index(number):] if not self.buffers[n].failed]
        playlist = self.ctx.runtime_dir / "cd-queue.m3u"
        playlist.parent.mkdir(parents=True, exist_ok=True)
        playlist.write_text("#EXTM3U\n" + "".join(u + "\n" for u in urls), "utf-8")
        await self.engine.command("set_property", "pause", False)
        await self.engine.command("loadlist", str(playlist), "replace")

    def capabilities(self) -> Capabilities:
        has = self.disc is not None
        return Capabilities(play=has, pause=has, stop=has, seek=has, next=has, previous=has)

    def details(self) -> dict[str, Any]:
        disc = None
        if self.disc:
            disc = dict(self.disc)
            disc["tracks"] = [
                dict(t, in_memory=self.buffers[t["number"]].done, failed=self.buffers[t["number"]].failed)
                for t in self.disc["tracks"]
            ]
        reading: dict[str, Any] = {}
        if self.disc:
            if self.reading is not None and self.reading in self.buffers:
                reading = {"track": self.reading, "percent": self.buffers[self.reading].percent, "done": False}
            elif all(b.done or b.failed for b in self.buffers.values()):
                reading = {"done": True}
        return {"status": self.status, "disc": disc, "reading": reading, "device": self.device}

    async def play(self) -> None:
        if self.owns_engine and self.np.state == PlayState.PAUSED:
            await self.resume()
        elif self.disc:
            await self.play_track(self.current or self._numbers()[0])
        else:
            raise SourceError("No audio CD in the drive", 409)

    async def next(self) -> None:
        numbers = self._numbers()
        if not numbers or self.current is None:
            raise SourceError("Nothing is playing", 409)
        i = numbers.index(self.current)
        if i + 1 >= len(numbers):
            raise SourceError("This is the last track")
        if self.owns_engine:
            await self.engine.command("playlist-next", "weak")
        else:
            await self.play_track(numbers[i + 1])

    async def previous(self) -> None:
        numbers = self._numbers()
        if not numbers or self.current is None:
            raise SourceError("Nothing is playing", 409)
        i = numbers.index(self.current)
        if (self.np.position or 0) > 3 or i == 0:
            await self.seek(0)
        elif self.owns_engine and self.base is not None and numbers[i - 1] >= self.base:
            await self.engine.command("playlist-prev", "weak")
        else:
            await self.play_track(numbers[i - 1])

    # ------------------------------------------------------------ mpv callbacks
    async def on_playlist_pos(self, index: int) -> None:
        if self.base is None or not self.disc:
            return
        numbers = [n for n in self._numbers()[self._numbers().index(self.base):] if not self.buffers[n].failed]
        if index < len(numbers) and numbers[index] != self.current:
            self._show(numbers[index])
            # Keep the drive working on whatever is now needed first.
            await self._reprioritise(numbers[index])
