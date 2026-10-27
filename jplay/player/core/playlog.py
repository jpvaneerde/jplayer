"""Posts every track that played long enough to a webhook (e.g. JPlay Spins)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .models import NowPlaying, PlayState

log = logging.getLogger(__name__)


class PlayLogger:
    def __init__(self, url: str, min_seconds: float, player_name: str):
        self.url = url
        self.min_seconds = min_seconds
        self.player_name = player_name
        self._key: tuple[str, str, str] | None = None
        self._source: str = ""
        self._started: float = 0.0
        self._played: float = 0.0
        self._last_tick: float | None = None
        self._snapshot: dict[str, Any] = {}
        self._logged = False

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def observe(self, source_id: str, np: NowPlaying) -> None:
        """Call on every state change of the active source."""
        if not self.enabled:
            return
        now = time.monotonic()
        key = np.track_key()
        if key != self._key or source_id != self._source:
            self._key, self._source = key, source_id
            self._started = time.time()
            self._played = 0.0
            self._last_tick = None
            self._logged = False
            self._snapshot = {
                "player": self.player_name,
                "source": source_id,
                "title": np.title,
                "artist": np.artist,
                "album": np.album,
                "duration": np.duration,
                "art_url": np.art_url,
                "extra": {k: v for k, v in np.extra.items() if isinstance(v, (str, int, float, bool))},
            }
        if np.state == PlayState.PLAYING:
            if self._last_tick is not None:
                self._played += min(now - self._last_tick, 5.0)
            self._last_tick = now
        else:
            self._last_tick = None
        threshold = self.min_seconds
        if np.duration:
            threshold = min(threshold, np.duration * 0.5)
        if not self._logged and np.title and self._played >= threshold:
            self._logged = True
            payload = dict(self._snapshot, started_at=self._started, played_seconds=round(self._played))
            asyncio.create_task(self._post(payload))

    async def _post(self, payload: dict[str, Any]) -> None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(self.url, json=payload)
                resp.raise_for_status()
        except Exception as exc:  # never let logging break playback
            log.warning("Play log webhook failed: %s", exc)
