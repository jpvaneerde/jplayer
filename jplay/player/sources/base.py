"""Base class every source (radio, Plex, CD, Spotify, Bluetooth) builds on."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..core.models import Capabilities, NowPlaying, PlayState

if TYPE_CHECKING:
    from ..core.context import AppContext

log = logging.getLogger(__name__)


class SourceError(Exception):
    """A user-facing error (bad request, source not configured ...)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Source:
    id: str = "base"
    name: str = "Source"

    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.cfg: dict[str, Any] = ctx.config.section(self.id)
        self.np = NowPlaying()
        self.available = False
        self.reason = ""

    # ------------------------------------------------------------ status
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled", True))

    @property
    def active(self) -> bool:
        return self.ctx.manager.active_id == self.id

    def capabilities(self) -> Capabilities:
        return Capabilities()

    def details(self) -> dict[str, Any]:
        """Source-specific status for the UI (devices, disc info, hints ...)."""
        return {}

    def info(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "available": self.available,
            "reason": self.reason,
            "active": self.active,
            "capabilities": self.capabilities().to_dict(),
            "now_playing": self.np.to_dict(),
            "details": self.details(),
        }

    def set_available(self, available: bool, reason: str = "") -> None:
        changed = (available, reason) != (self.available, self.reason)
        self.available, self.reason = available, reason
        if changed:
            self.ctx.manager.notify(significant=True)

    def update(self, significant: bool = True, **fields: Any) -> None:
        """Change now-playing fields and push the change to clients."""
        for key, value in fields.items():
            setattr(self.np, key, value)
        self.ctx.manager.source_changed(self, significant)

    def reset_now_playing(self, **fields: Any) -> None:
        self.np = NowPlaying(**fields)
        self.ctx.manager.source_changed(self, True)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Called once at startup. Probe dependencies and set `available`."""

    async def shutdown(self) -> None:
        """Called once at exit."""

    async def activate(self) -> None:
        """This source just became the active one (the previous one has released the DAC)."""

    async def deactivate(self) -> None:
        """Another source takes over: stop playback and release the audio device."""

    async def output_changed(self) -> None:
        """The user picked another DAC (or 'auto' found a new one). Already deactivated."""

    # ------------------------------------------------------------ transport
    async def play(self) -> None:
        raise SourceError(f"{self.name} cannot play from here")

    async def pause(self) -> None:
        raise SourceError(f"{self.name} cannot pause")

    async def toggle(self) -> None:
        if self.np.state == PlayState.PLAYING:
            await self.pause()
        else:
            await self.play()

    async def stop(self) -> None:
        raise SourceError(f"{self.name} cannot stop")

    async def next(self) -> None:
        raise SourceError(f"{self.name} has no next track")

    async def previous(self) -> None:
        raise SourceError(f"{self.name} has no previous track")

    async def seek(self, position: float) -> None:
        raise SourceError(f"{self.name} cannot seek")
