"""Every question to Shazam goes through here, so a display never asks too often.

Shazam stops answering a display that asks a lot, and while it limits, it often still answers, just without a
song. So: never two questions at once and at least MIN_GAP seconds apart. After a refusal: a pause (1 minute,
doubling up to 15), and then an hour of slow asking (SLOW_GAP), so an empty answer doesn't count as "fine again".
Only a song found ends the slow hour early. While Shazam refuses, the microphone may ask AudD instead (optional,
see audd.py).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

MIN_GAP = 4.0              # seconds between two questions
SLOW_GAP = 30.0            # ... in the hour after a refusal
SLOW_FOR = 3600
ANSWER_WITHIN = 20         # seconds; Shazam normally answers in one or two
BACKOFF_FIRST, BACKOFF_MAX = 60, 900

Fallback = Callable[[bytes], Awaitable[dict]]


def _client():
    """Shazam without its own retries: a refusal should reach us at once (we wait, instead of hammering)."""
    from shazamio import Shazam

    try:
        from aiohttp_retry import ExponentialRetry
        from shazamio.client import HTTPClient

        return Shazam(http_client=HTTPClient(retry_options=ExponentialRetry(attempts=1)))
    except Exception:          # an older shazamio: its defaults
        return Shazam()


class Recognizer:
    def __init__(self, client: Any = None):
        self._client = client
        self._lock: asyncio.Lock | None = None
        self.last_ask = 0.0
        self.paused_until = 0.0
        self.slow_until = 0.0
        self.backoff = 0
        self.status: dict[str, Any] = {"asked": 0, "answered": 0, "found": 0, "refused": 0, "skipped": 0,
                                       "backup_used": 0, "last_error": "", "last_error_at": None, "by": {}}

    @property
    def client(self):
        if self._client is None:
            self._client = _client()
        return self._client

    def view(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        state = ("waiting (Shazam refused)" if self.paused_until > now else
                 "slow (Shazam refused in the last hour)" if self.slow_until > now else "ok")
        return {**self.status, "waiting_seconds": max(0, round(self.paused_until - now)),
                "slow_minutes": max(0, round((self.slow_until - now) / 60)), "state": state}

    async def recognize(self, audio: bytes, by: str = "mic", fallback: Fallback | None = None) -> dict:
        """Shazam's answer; while Shazam refuses: the backup's answer (when given one), else {}."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.time()
            if now < self.paused_until:
                self.status["skipped"] += 1
                return await self._backup(audio, fallback)
            slow = now < self.slow_until
            gap = self.last_ask + (SLOW_GAP if slow else MIN_GAP) - now
            if gap > 0:
                if slow:                       # not now: the caller tries again later
                    self.status["skipped"] += 1
                    return {}
                await asyncio.sleep(gap)
            self.last_ask = time.time()
            self.status["asked"] += 1
            self.status["by"][by] = self.status["by"].get(by, 0) + 1
            try:
                out = await asyncio.wait_for(self.client.recognize(audio), ANSWER_WITHIN)
            except Exception as e:
                self._refused(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
                return await self._backup(audio, fallback)
            if not isinstance(out, dict) or ("matches" not in out and "track" not in out):
                self._refused(f"unexpected answer: {str(out)[:160]}")
                return await self._backup(audio, fallback)
            self.status["answered"] += 1
            if out.get("track"):
                self.status["found"] += 1
                self.backoff = 0
                self.slow_until = 0.0
            return out

    async def _backup(self, audio: bytes, fallback: Fallback | None) -> dict:
        if fallback is None:
            return {}
        try:
            out = await fallback(audio)
        except Exception as e:
            print(f"Backup recognition failed: {type(e).__name__}: {e}")
            return {}
        if out:
            self.status["backup_used"] += 1
        return out or {}

    def _refused(self, why: str) -> None:
        now = time.time()
        self.backoff = min(BACKOFF_MAX, self.backoff * 2 if self.backoff else BACKOFF_FIRST)
        self.paused_until = now + self.backoff
        self.slow_until = now + self.backoff + SLOW_FOR
        self.status.update(refused=self.status["refused"] + 1, last_error=why, last_error_at=now)
        print(f"Shazam: {why}; asking again in {self.backoff} s, then slowly for an hour")
