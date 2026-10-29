"""Every question to Shazam goes through here, so a display never asks too often.

Shazam stops answering an address that asks a lot (the microphone every few seconds, radio streams on top, and
every display in the house behind the same internet address). Once it stops, nothing is recognised for a long
while, records included. So: never two questions at once, at least MIN_GAP seconds apart, and when Shazam
refuses or doesn't answer, wait (1 minute, doubling up to 15) before asking again.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

MIN_GAP = 4.0              # seconds between two questions
ANSWER_WITHIN = 20         # seconds; Shazam normally answers in one or two
BACKOFF_FIRST, BACKOFF_MAX = 60, 900


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
        self.backoff = 0
        self.status: dict[str, Any] = {"asked": 0, "answered": 0, "found": 0, "refused": 0, "skipped": 0,
                                       "last_error": "", "last_error_at": None, "by": {}}

    @property
    def client(self):
        if self._client is None:
            self._client = _client()
        return self._client

    def view(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {**self.status, "waiting_seconds": max(0, round(self.paused_until - now)),
                "state": "waiting (Shazam refused)" if self.paused_until > now else "ok"}

    async def recognize(self, audio: bytes, by: str = "mic") -> dict:
        """Shazam's answer, or {} while waiting after a refusal (counted as "skipped")."""
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now = time.time()
            if now < self.paused_until:
                self.status["skipped"] += 1
                return {}
            gap = self.last_ask + MIN_GAP - now
            if gap > 0:
                await asyncio.sleep(gap)
            self.last_ask = time.time()
            self.status["asked"] += 1
            self.status["by"][by] = self.status["by"].get(by, 0) + 1
            try:
                out = await asyncio.wait_for(self.client.recognize(audio), ANSWER_WITHIN)
            except Exception as e:
                return self._refused(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)
            if not isinstance(out, dict) or ("matches" not in out and "track" not in out):
                return self._refused(f"unexpected answer: {str(out)[:160]}")
            self.status["answered"] += 1
            self.backoff = 0
            if out.get("track"):
                self.status["found"] += 1
            return out

    def _refused(self, why: str) -> dict:
        self.backoff = min(BACKOFF_MAX, self.backoff * 2 if self.backoff else BACKOFF_FIRST)
        self.paused_until = time.time() + self.backoff
        self.status.update(refused=self.status["refused"] + 1, last_error=why, last_error_at=time.time())
        print(f"Shazam: {why}; asking again in {self.backoff} s")
        return {}
