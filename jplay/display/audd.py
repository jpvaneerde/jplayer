"""AudD (audd.io): the backup for the microphone while Shazam refuses. Optional: only with a token in Settings.

AudD is paid per request (a few hundred free to try, then about $5 per 1,000), so it is asked sparingly: only
while Shazam refuses, never more than once every MIN_GAP seconds and at most DAILY_MAX times a day; the
microphone loop already asks only once per song. The answer is turned into the shape Shazam's answers have, so
the rest of the display doesn't need to know which one named the song.
"""
from __future__ import annotations

import time
from typing import Any

import httpx

URL = "https://api.audd.io/"
MIN_GAP = 20           # seconds between two questions
DAILY_MAX = 200        # questions per day, at most


class AudD:
    def __init__(self):
        self.last_ask = 0.0
        self.day = ""
        self.today = 0
        self.status: dict[str, Any] = {"asked": 0, "found": 0, "last_error": "", "last_error_at": None,
                                       "last_song": "", "today": 0, "daily_max": DAILY_MAX}

    def view(self) -> dict:
        return {**self.status, "today": self.today}

    def _allowed(self, now: float) -> bool:
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        if day != self.day:
            self.day, self.today = day, 0
        return now - self.last_ask >= MIN_GAP and self.today < DAILY_MAX

    async def recognize(self, audio: bytes, token: str, client: httpx.AsyncClient | None = None) -> dict:
        """A Shazam-shaped answer ({"track": ..., "matches": [...]}), {} for "no song", or {} when not asked."""
        now = time.time()
        if not token or not self._allowed(now):
            return {}
        self.last_ask = now
        self.today += 1
        self.status["asked"] += 1
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=30) as c:
                    r = await self._post(c, audio, token)
            else:
                r = await self._post(client, audio, token)
            data = r.json()
        except Exception as e:
            self._error(f"{type(e).__name__}: {e}")
            return {}
        if data.get("status") != "success":
            err = data.get("error") or {}
            self._error(f"{err.get('error_code', '')} {err.get('error_message', data)}".strip()[:200])
            return {}
        res = data.get("result")
        if not res:
            return {"matches": []}
        self.status["found"] += 1
        self.status["last_song"] = f"{res.get('artist', '')} - {res.get('title', '')}"
        return to_shazam(res)

    @staticmethod
    async def _post(c: httpx.AsyncClient, audio: bytes, token: str) -> httpx.Response:
        return await c.post(URL, data={"api_token": token, "return": "apple_music,spotify"},
                            files={"file": ("clip.wav", audio, "audio/wav")})

    def _error(self, why: str) -> None:
        self.status.update(last_error=why, last_error_at=time.time())
        print(f"AudD: {why}")


def _seconds(timecode: str | None) -> float | None:
    """'01:23' -> 83.0"""
    try:
        parts = [float(p) for p in str(timecode).split(":")]
    except (TypeError, ValueError):
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return total


def to_shazam(res: dict) -> dict:
    """AudD's result in the shape of a Shazam answer (title, subtitle = artist, images, Album/Released, isrc)."""
    apple = res.get("apple_music") or {}
    spotify = res.get("spotify") or {}
    cover = ""
    art = (apple.get("artwork") or {}).get("url") or ""
    if art:
        cover = art.replace("{w}", "600").replace("{h}", "600")
    else:
        imgs = ((spotify.get("album") or {}).get("images") or [])
        cover = imgs[0].get("url", "") if imgs else ""
    isrc = apple.get("isrc") or (spotify.get("external_ids") or {}).get("isrc") or ""
    genres = apple.get("genreNames") or []
    meta = [{"title": "Album", "text": res.get("album") or ""}, {"title": "Released", "text": res.get("release_date") or ""}]
    offset = _seconds(res.get("timecode"))
    return {
        "matches": [{"offset": offset}] if offset is not None else [{}],
        "track": {"title": res.get("title") or "", "subtitle": res.get("artist") or "",
                  "images": {"coverart": cover} if cover else {}, "sections": [{"metadata": meta}],
                  "genres": {"primary": genres[0]} if genres else {}, "isrc": isrc, "key": "",
                  "source": "audd"},
    }
