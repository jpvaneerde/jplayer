"""Common behaviour for sources that play through the shared mpv engine."""
from __future__ import annotations

import logging
from typing import Any

from ..core.models import PlayState
from .base import Source, SourceError

log = logging.getLogger(__name__)


class EngineSource(Source):
    """Radio, Plex and CD: mpv does the decoding, this class maps its state to NowPlaying."""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._paused = False
        self._file_started = False

    @property
    def engine(self):
        return self.ctx.engine

    def engine_ready(self) -> tuple[bool, str]:
        if not self.engine.available:
            return False, f"'{self.engine.binary}' is not installed"
        return True, ""

    async def claim(self) -> None:
        """Become the active source and make sure mpv runs for us."""
        await self.ctx.manager.activate(self.id)
        await self.engine.acquire(self)
        for name, value in {"network-timeout": 15, **self.engine_properties()}.items():
            await self.engine.command("set_property", name, value)

    def engine_properties(self) -> dict[str, Any]:
        """mpv settings this source needs while it owns the engine."""
        return {}

    async def deactivate(self) -> None:
        await self.engine.release(self)
        self._paused = False
        self._file_started = False
        self.update(state=PlayState.STOPPED, position=None)

    @property
    def owns_engine(self) -> bool:
        return self.engine.listener is self and self.engine.running

    async def cmd(self, *args: Any) -> Any:
        if not self.owns_engine:
            raise SourceError(f"{self.name} is not playing", 409)
        return await self.engine.command(*args)

    # ------------------------------------------------------------ transport defaults
    async def pause(self) -> None:
        await self.cmd("set_property", "pause", True)

    async def resume(self) -> None:
        await self.cmd("set_property", "pause", False)

    async def stop(self) -> None:
        if self.owns_engine:
            await self.engine.command("stop")
        self.update(state=PlayState.STOPPED, position=None)

    async def seek(self, position: float) -> None:
        await self.cmd("seek", max(0.0, float(position)), "absolute")

    # ------------------------------------------------------------ mpv callbacks
    async def on_mpv_property(self, name: str, value: Any) -> None:
        if name == "pause":
            self._paused = bool(value)
            if self.np.state in (PlayState.PLAYING, PlayState.PAUSED):
                self.update(state=PlayState.PAUSED if self._paused else PlayState.PLAYING)
        elif name == "time-pos":
            if value is not None:
                self.update(significant=False, position=round(float(value), 1))
        elif name == "duration":
            self.update(duration=round(float(value), 1) if value else None)
        elif name == "playlist-pos":
            if value is not None and value >= 0:
                await self.on_playlist_pos(int(value))
        elif name == "metadata":
            await self.on_metadata(value or {})
        elif name == "idle-active":
            # Only an idle that follows a started file means "playback ended". The initial
            # idle value mpv reports right after it starts is ignored.
            if value and self._file_started:
                self._file_started = False
                self.update(state=PlayState.STOPPED, position=None)
                await self.on_idle()
        elif name == "audio-params":
            if value:
                self.update(
                    extra={
                        **self.np.extra,
                        "format": value.get("format"),
                        "samplerate": value.get("samplerate"),
                        "channels": value.get("channel-count"),
                    }
                )

    async def on_mpv_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "start-file":
            self._file_started = True
            self.update(state=PlayState.LOADING, position=None)
        elif name == "file-loaded":
            self.update(state=PlayState.PAUSED if self._paused else PlayState.PLAYING)
        elif name == "end-file" and event.get("reason") == "error":
            msg = event.get("file_error") or "playback error"
            log.warning("%s: %s", self.name, msg)
            self.update(extra={**self.np.extra, "error": msg})
        elif name == "engine-exit":
            self._file_started = False
            self.update(state=PlayState.STOPPED, position=None)

    # ------------------------------------------------------------ hooks for subclasses
    async def on_playlist_pos(self, index: int) -> None:
        pass

    async def on_metadata(self, metadata: dict[str, Any]) -> None:
        pass

    async def on_idle(self) -> None:
        pass
