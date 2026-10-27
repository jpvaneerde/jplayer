"""Shared data shapes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class PlayState(str, Enum):
    STOPPED = "stopped"
    LOADING = "loading"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class NowPlaying:
    """What a source is doing right now. Every source fills in the same shape."""

    state: PlayState = PlayState.STOPPED
    title: str = ""
    artist: str = ""
    album: str = ""
    art_url: str = ""
    duration: float | None = None  # seconds, None for live streams
    position: float | None = None  # seconds
    track_number: int | None = None
    track_count: int | None = None
    queue_index: int | None = None
    queue_length: int | None = None
    # Free-form source details (station name, codec, Bluetooth device, buffer progress ...)
    extra: dict[str, Any] = field(default_factory=dict)

    def track_key(self) -> tuple[str, str, str]:
        """Identity of the current track, used to detect track changes."""
        return (self.title, self.artist, self.album)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class Capabilities:
    play: bool = False
    pause: bool = False
    stop: bool = False
    next: bool = False
    previous: bool = False
    seek: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)
