"""Your own music collection as the first place to look: the fingerprint service (jplay/fingerprint, in Kubernetes).

It costs nothing to ask and has no limits, so the microphone asks it for every clip while unknown music plays, with
a longer clip (this one and the one before: more to match on). Only when it doesn't know the song is Shazam asked.
The answer is turned into the shape Shazam's answers have, like AudD's (audd.py).
"""
from __future__ import annotations

import io
import time
import wave
from typing import Any

import httpx

MAX_SECONDS = 24          # the longest clip sent
PAUSE_AFTER_ERROR = 60    # seconds; the service is away (the cluster restarting): don't wait on it for every clip


def join_wavs(first: bytes, second: bytes, max_seconds: float = MAX_SECONDS) -> bytes:
    """Two WAV clips (the same format) as one, at most max_seconds long (the end is kept)."""
    try:
        with wave.open(io.BytesIO(first)) as a, wave.open(io.BytesIO(second)) as b:
            if (a.getnchannels(), a.getsampwidth(), a.getframerate()) != (b.getnchannels(), b.getsampwidth(), b.getframerate()):
                return second
            params = b.getparams()
            frames = a.readframes(a.getnframes()) + b.readframes(b.getnframes())
    except (wave.Error, EOFError):
        return second
    per_second = params.framerate * params.nchannels * params.sampwidth
    frames = frames[-int(max_seconds * per_second):]
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(params.nchannels)
        w.setsampwidth(params.sampwidth)
        w.setframerate(params.framerate)
        w.writeframes(frames)
    return out.getvalue()


class Collection:
    def __init__(self):
        self.paused_until = 0.0
        self.status: dict[str, Any] = {"asked": 0, "found": 0, "last_ms": None, "last_error": "", "last_error_at": None,
                                       "last_song": ""}

    def view(self) -> dict:
        return {**self.status, "waiting_seconds": max(0, round(self.paused_until - time.time()))}

    async def recognize(self, audio: bytes, url: str, token: str = "", client: httpx.AsyncClient | None = None) -> dict:
        """A Shazam-shaped answer, {"matches": []} for "not in the collection", or {} when it couldn't be asked."""
        if not url or time.time() < self.paused_until:
            return {}
        base = url.rstrip("/")
        self.status["asked"] += 1
        started = time.monotonic()
        headers = {"Content-Type": "audio/wav", **({"Authorization": f"Bearer {token}"} if token else {})}
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=15) as c:
                    r = await c.post(base + "/api/recognize", content=audio, headers=headers)
            else:
                r = await client.post(base + "/api/recognize", content=audio, headers=headers)
            if r.status_code == 401:
                raise RuntimeError("the token is wrong (Settings: Your collection)")
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self.paused_until = time.time() + PAUSE_AFTER_ERROR
            self.status.update(last_error=f"{type(e).__name__}: {e}"[:200], last_error_at=time.time())
            return {}
        self.status["last_ms"] = round((time.monotonic() - started) * 1000)
        if not data.get("found"):
            return {"matches": []}
        self.status["found"] += 1
        song = data["song"]
        self.status["last_song"] = f"{song.get('artist', '')} - {song.get('title', '')}"
        return to_shazam(data, base)


def to_shazam(data: dict, base: str) -> dict:
    s = data["song"]
    meta = [{"title": "Album", "text": s.get("album") or ""}, {"title": "Released", "text": s.get("year") or ""}]
    return {
        "matches": [{"offset": data.get("offset")}],
        "track": {"title": s.get("title") or "", "subtitle": s.get("artist") or s.get("albumartist") or "",
                  "images": {"coverart": base + s["cover"]} if s.get("cover") else {},
                  "sections": [{"metadata": meta}], "genres": {}, "key": "", "isrc": "",
                  "duration": s.get("duration"), "source": "collection"},
    }
