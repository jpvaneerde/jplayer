"""Fan-out of events to WebSocket clients and other listeners."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class EventHub:
    """Each subscriber gets its own bounded queue; slow clients drop old messages."""

    def __init__(self, max_queue: int = 100):
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._max_queue = max_queue

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._max_queue)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, event_type: str, data: Any) -> None:
        msg = {"type": event_type, "data": data}
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest message for this slow client
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
