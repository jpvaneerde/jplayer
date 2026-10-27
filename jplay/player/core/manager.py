"""Owns the sources, decides which one is active, and publishes state."""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any

from .models import NowPlaying
from .playlog import PlayLogger

if TYPE_CHECKING:
    from ..sources.base import Source
    from .context import AppContext

log = logging.getLogger(__name__)

TRANSPORT = ("play", "pause", "toggle", "stop", "next", "previous")


class SourceManager:
    """Exactly one source is active at a time; it alone may use the DAC.

    Switching deactivates the old source first (it stops and releases the audio device)
    and only then activates the new one. Sources that start on their own (Spotify Connect,
    a phone streaming over Bluetooth, an inserted CD) call `activate()` to claim the DAC.
    """

    def __init__(self, ctx: "AppContext", sources: list["Source"]):
        self.ctx = ctx
        self.sources: dict[str, Source] = {s.id: s for s in sources}
        self.active_id: str | None = None
        self.volume: int | None = None
        self._switch_lock = asyncio.Lock()
        self._dirty = asyncio.Event()
        self._urgent = asyncio.Event()
        self._last_sent = 0.0
        self._publisher: asyncio.Task[None] | None = None
        player_name = ctx.config["server"]["name"]
        hook = ctx.config["webhook"]
        self.playlog = PlayLogger(hook.get("url", ""), float(hook.get("min_seconds", 30)), player_name)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        self._publisher = asyncio.create_task(self._publish_loop(), name="state-publisher")
        if self.ctx.output is not None:
            await self.ctx.output.start()
        self.volume = await self.ctx.volume.get()
        for source in self.sources.values():
            if not source.enabled:
                source.set_available(False, "Disabled in config")
                continue
            try:
                await source.start()
            except Exception as exc:
                log.exception("Source %s failed to start", source.id)
                source.set_available(False, f"Failed to start: {exc}")
        self.notify(True)

    async def shutdown(self) -> None:
        if self.active_id:
            with contextlib.suppress(Exception):
                await self.sources[self.active_id].deactivate()
        for source in self.sources.values():
            with contextlib.suppress(Exception):
                await source.shutdown()
        await self.ctx.engine.shutdown()
        if self.ctx.output is not None:
            await self.ctx.output.shutdown()
        if self._publisher:
            self._publisher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._publisher

    # ------------------------------------------------------------ switching
    @property
    def active(self) -> "Source | None":
        return self.sources.get(self.active_id) if self.active_id else None

    def get(self, source_id: str) -> "Source":
        from ..sources.base import SourceError

        source = self.sources.get(source_id)
        if source is None:
            raise SourceError(f"Unknown source '{source_id}'", 404)
        return source

    async def activate(self, source_id: str) -> "Source":
        from ..sources.base import SourceError

        source = self.get(source_id)
        if not source.available:
            raise SourceError(f"{source.name} is not available: {source.reason}", 409)
        out = self.ctx.output
        if out is not None and not out.present:
            if out.setting == "auto":
                raise SourceError("No DAC found. Switch the DAC on or plug it in.", 409)
            raise SourceError(f"The DAC ({out.setting}) isn't connected. Switch it on or plug it in.", 409)
        async with self._switch_lock:
            if self.active_id == source_id:
                return source
            previous = self.active
            if previous is not None:
                log.info("Switching %s -> %s", previous.id, source_id)
                try:
                    await previous.deactivate()
                except Exception:
                    log.exception("Deactivating %s failed", previous.id)
            else:
                log.info("Activating %s", source_id)
            self.active_id = source_id
            try:
                await source.activate()
            except Exception:
                log.exception("Activating %s failed", source_id)
            self.ctx.store.set("last_source", source_id)
        self.notify(True)
        return source

    async def release(self, source_id: str) -> None:
        """A source that ended by itself (the phone left Spotify or Bluetooth): nothing is active any more.

        Unlike deactivate_all this doesn't stop the source; it already stopped.
        """
        async with self._switch_lock:
            if self.active_id != source_id:
                return
            log.info("%s ended; nothing active", source_id)
            self.active_id = None
        self.notify(True)

    async def deactivate_all(self) -> None:
        async with self._switch_lock:
            if self.active is not None:
                await self.active.deactivate()
            self.active_id = None
        self.notify(True)

    async def transport(self, command: str, *args: Any) -> None:
        from ..sources.base import SourceError

        source = self.active
        if source is None:
            raise SourceError("Nothing is playing. Pick a source first.", 409)
        if command == "seek":
            await source.seek(float(args[0]))
        elif command in TRANSPORT:
            await getattr(source, command)()
        else:
            raise SourceError(f"Unknown command '{command}'")

    async def set_volume(self, percent: int) -> int | None:
        self.volume = await self.ctx.volume.set(percent)
        self.notify(True)
        return self.volume

    # ------------------------------------------------------------ state publishing
    def source_changed(self, source: "Source", significant: bool) -> None:
        if source.id == self.active_id:
            self.playlog.observe(source.id, source.np)
        self.notify(significant)

    def notify(self, significant: bool = True) -> None:
        self._dirty.set()
        if significant:
            self._urgent.set()

    def snapshot(self) -> dict[str, Any]:
        active = self.active
        return {
            "player": self.ctx.config["server"]["name"],
            "active": self.active_id,
            "now_playing": (active.np if active else NowPlaying()).to_dict(),
            "capabilities": active.capabilities().to_dict() if active else {},
            "volume": self.volume,
            "volume_available": self.ctx.volume.available,
            "output": self.ctx.output.info() if self.ctx.output is not None else None,
            "sources": [s.info() for s in self.sources.values()],
        }

    async def _publish_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await self._dirty.wait()
            if not self._urgent.is_set():
                # Position-only updates: at most one per second.
                delay = self._last_sent + 1.0 - loop.time()
                if delay > 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._urgent.wait(), delay)
            await asyncio.sleep(0.03)  # coalesce bursts of changes
            self._dirty.clear()
            self._urgent.clear()
            self._last_sent = loop.time()
            if self.ctx.hub.subscriber_count:
                try:
                    self.ctx.hub.publish("state", self.snapshot())
                except Exception:
                    log.exception("Publishing state failed")
